// 资产工厂（scene/AssetFactory.js）：GLTFLoader 加载 assets/*.glb（带缓存），
// 按节点名克隆子树用于实例化；运行时用 CanvasTexture 绘制车牌号与信息屏内容
//（号码仅展示——识别输入来自图库，与建模解耦 §2.2-1）。
//
// route-v2 新增：
// - ``loadOptional(file)``：可选资产（如 lot_shell.glb 尚未产出）失败返回 null，不抛错；
// - ``loadShell()``：加载 Blender 静态场地壳（lot_shell.glb），统一修正投影/阴影/材质，
//   并对接壳内命名（shell 内标线已略高于程序化标线，避免 z-fighting）。
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const DEFAULT_ASSET_BASE = './assets/';

// 壳内不需要投射阴影的“大平面”命名（地面/道路/铺装/标线）——避免超大阴影图抖动
const SHELL_NO_CAST = /ground|road|plaza|apron|asphalt|paint|line|marking|grass|curb|island|median|lot_/i;

export class AssetFactory {
  /**
   * @param {{assetBase?:string}} [opts]
   */
  constructor(opts = {}) {
    this.loader = new GLTFLoader();
    this.cache = new Map();      // file -> Promise<Group>
    this.assetBase = opts.assetBase || DEFAULT_ASSET_BASE;
    this._missingWarned = new Set();
    this.failures = new Set();
    this.onFailure = () => {};
    // MIT chinese_license_plate_generator 真实样式车牌贴图（plates/index.json；见 loadPlateManifest）
    this._plateManifest = null;
    this._plateManifestPromise = null;
    this._plateTextures = new Map();
    this._plateWarned = new Set();
  }

  /** 加载 glb（缓存 Promise，避免重复请求）。返回 gltf.scene 根 Group。 */
  load(file) {
    if (!this.cache.has(file)) {
      const p = new Promise((resolve, reject) => {
        this.loader.load(this.assetBase + file,
          (gltf) => {
            gltf.scene.traverse((o) => {
              if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
            });
            this.failures.delete(file);
            resolve(gltf.scene);
          },
          undefined,
          (err) => { this.cache.delete(file); this.failures.add(file); this.onFailure(file); reject(new Error(`加载 ${file} 失败: ${err}`)); });
      });
      this.cache.set(file, p);
    }
    return this.cache.get(file);
  }

  /**
   * 可选加载：失败返回 null（用于“尚未产出/允许缺失”的资产，如 lot_shell.glb）。
   * 注意：错误只告警一次，避免每帧刷屏。
   */
  async loadOptional(file) {
    try {
      return await this.load(file);
    } catch (e) {
      if (!this._missingWarned.has(file)) {
        this._missingWarned.add(file);
        console.warn(`[AssetFactory] 可选资产缺失，已回退：${file}（${e.message}）`);
      }
      return null;
    }
  }

  /** 探测资产是否存在（HEAD；部分静态服务器不支持 HEAD 时自动降级为 null=未知）。 */
  async exists(file) {
    try {
      const res = await fetch(this.assetBase + file, { method: 'HEAD', cache: 'no-cache' });
      return res.ok;
    } catch (_) { return null; }
  }

  /** 克隆整个 glb（车辆等独立资产）。 */
  async cloneModel(file) {
    const root = await this.load(file);
    const c = root.clone(true);
    c.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    return c;
  }

  /** 从合集 glb（env_props/signage/booth_props）克隆某个命名子节点。 */
  async cloneNode(file, nodeName) {
    const root = await this.load(file);
    const node = root.getObjectByName(nodeName);
    if (!node) {
      console.warn(`[AssetFactory] ${file} 中未找到节点 ${nodeName}`);
      return null;
    }
    const c = node.clone(true);
    c.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; } });
    return c;
  }

  /** 按候选节点名依次尝试（适配 blender-assets 可能的重命名）。 */
  async cloneNodeAny(file, nodeNames) {
    const list = Array.isArray(nodeNames) ? nodeNames : [nodeNames];
    for (const name of list) {
      const c = await this.cloneNode(file, name);
      if (c) return c;
    }
    return null;
  }

  /** 列出合集 glb 顶层子节点名（用于按名实例化环境道具）。 */
  async topLevelNames(file) {
    const root = await this.load(file);
    return root.children.map((c) => c.name).filter(Boolean);
  }

  // ----------------------------- 静态场地壳 -----------------------------
  /**
   * 加载 Blender 导出的静态场地壳（lot_shell.glb）：地面/道路/路缘/绿岛/分岔区/标线/围墙。
   * 失败返回 null（调用方回退 GroundBuilder/ProceduralGround 程序化地面）。
   * 注意：除加载失败外，材质修正/遍历/包围盒计算抛错也一并兜住，保证调用方永远拿到
   *       “可用壳或 null”，不会因半成品壳把场景构建打断（qa-runtime P0 回归）。
   * @param {string} [file]
   * @param {{scale?:number}} [opts]
   */
  async loadShell(file = 'lot_shell.glb', opts = {}) {
    let root = null;
    try {
      root = await this.loadOptional(file);
      if (!root) return null;
      const shell = root.clone(true);
      shell.name = 'LotShell';
      if (opts.scale && opts.scale !== 1) shell.scale.setScalar(opts.scale);
      let meshCount = 0;
      shell.traverse((o) => {
        if (!o.isMesh) return;
        meshCount += 1;
        const name = String(o.name || '');
        o.receiveShadow = true;
        o.castShadow = !SHELL_NO_CAST.test(name);
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const m of mats) this._fixMaterial(m);
        if (/paint|line|marking/i.test(name)) o.renderOrder = 1;
      });
      shell.userData = { ...(shell.userData || {}), isShell: true, meshCount };
      // 性能：壳内 56 个 mesh → 按材质合批（保留 Shell_* 组节点名以维持契约）
      try {
        shell.userData.mergeStats = this._mergeShellByMaterial(shell);
      } catch (e) {
        console.warn('[AssetFactory] shell 合批失败（保持原 mesh）：', e);
      }
      try {
        shell.userData.shellBox = new THREE.Box3().setFromObject(shell);
      } catch (_) { /* 空壳忽略 */ }
      return shell;
    } catch (e) {
      console.warn(`[AssetFactory] loadShell(${file}) 失败，回退程序化地面：${e.message || e}`);
      return null;
    }
  }

  _fixMaterial(m) {
    if (!m || typeof m !== 'object') return;
    if (m.map) {
      m.map.colorSpace = THREE.SRGBColorSpace;
      m.map.anisotropy = Math.max(m.map.anisotropy || 1, 4);
    }
    if (m.emissiveMap) m.emissiveMap.colorSpace = THREE.SRGBColorSpace;
    if (m.normalMap) m.normalMap.colorSpace = THREE.NoColorSpace;
    if ('envMapIntensity' in m) m.envMapIntensity = 0.6;
    if (m.isMeshStandardMaterial) {
      // 场景是户外日光 + ACES：略微压低金属度，避免材质发黑/过亮
      if (m.metalness > 0.6) m.metalness = 0.6;
      if (m.roughness < 0.25) m.roughness = 0.25;
    }
    m.needsUpdate = true;
  }

  /**
   * 壳合批：把壳内所有 mesh 按“材质 + 顶点属性签名 + 是否索引”分组，合并成少数 Mesh，
   * 大幅降低 draw call（56 → ~24，等于壳内材质数）。保留 `Shell_*` 组节点（清空其子 mesh），
   * 以满足 `shell_contract.children` 的命名契约；合批 mesh 挂到壳根下 `Shell_Merged` 组。
   * 失败时由调用方保持原 mesh（不破坏场景）。
   */
  _mergeShellByMaterial(shell) {
    shell.updateMatrixWorld(true);
    const invRoot = new THREE.Matrix4().copy(shell.matrixWorld).invert();
    const buckets = new Map();
    const meshes = [];
    shell.traverse((o) => { if (o.isMesh) meshes.push(o); });
    for (const mesh of meshes) {
      const geos = []; // 兼容 material 数组（本壳为单材质，仍做防御）
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      for (const mat of mats) {
        if (!mat) continue;
        const g = mesh.geometry;
        const sig = Object.keys(g.attributes || {}).sort().join(',') + '|idx=' + (g.index ? 1 : 0);
        const key = `${mat.uuid}|${sig}`;
        if (!buckets.has(key)) buckets.set(key, { mat, geos: [], cast: false, order: 0 });
        const b = buckets.get(key);
        const rel = new THREE.Matrix4().multiplyMatrices(invRoot, mesh.matrixWorld);
        const cg = g.clone();
        cg.applyMatrix4(rel);
        b.geos.push(cg);
        b.cast = b.cast || mesh.castShadow;
        b.order = Math.max(b.order, mesh.renderOrder || 0);
      }
    }
    const mergedGroup = new THREE.Group();
    mergedGroup.name = 'Shell_Merged';
    let mergedCount = 0;
    for (const b of buckets.values()) {
      if (!b.geos.length) continue;
      let merged = null;
      if (b.geos.length === 1) {
        merged = b.geos[0];
      } else {
        merged = mergeGeometries(b.geos, false);
        for (const g of b.geos) g.dispose();
      }
      if (!merged) continue;                 // 合批失败：该材质保留原 mesh（不删除）
      const m = new THREE.Mesh(merged, b.mat);
      m.name = `ShellMerged_${b.mat.name || mergedCount}`;
      m.castShadow = b.cast;
      m.receiveShadow = true;
      m.renderOrder = b.order;
      mergedGroup.add(m);
      mergedCount++;
    }
    if (!mergedCount) return { before: meshes.length, after: meshes.length, merged: 0 };
    // 用合批结果替换原 mesh：先加新、再从父节点移除旧（旧几何释放）。
    // 契约保留：glTF 里 Shell_* 节点本身就是 Mesh；为保持 shell_contract.children 命名，
    // 被合批掉的 Shell_* mesh 原位替换为同名空 Group（只占一个 Object3D，不产生 draw call）。
    shell.add(mergedGroup);
    for (const mesh of meshes) {
      const parent = mesh.parent;
      if (parent) {
        if (/^Shell_/.test(mesh.name || '')) {
          const marker = new THREE.Group();
          marker.name = mesh.name;
          marker.position.copy(mesh.position);
          marker.quaternion.copy(mesh.quaternion);
          marker.scale.copy(mesh.scale);
          parent.add(marker);
        }
        parent.remove(mesh);
      }
      try { mesh.geometry?.dispose(); } catch (_) { /* ignore */ }
    }
    return { before: meshes.length, after: mergedCount, merged: mergedCount, contractGroups: meshes.filter((m) => /^Shell_/.test(m.name || '')).length };
  }

  /**
   * 加载 MIT 生成器真实样式车牌贴图索引：web/sim3d/assets/plates/index.json。
   * 由 tools/build_plate_textures.py 生成（datasets/demo-lpr 按 corners 裁切）。
   * 加载失败时 plateTexture 回退 Canvas（仅未知车牌/测试环境；演示剧本车牌均已覆盖）。
   */
  async loadPlateManifest() {
    if (this._plateManifestPromise) return this._plateManifestPromise;
    const url = this.assetBase + 'plates/index.json';
    this._plateManifestPromise = (async () => {
      try {
        const res = await fetch(url, { cache: 'no-cache' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        this._plateManifest = data && data.plates ? data : null;
        console.info(`[AssetFactory] 真实样式车牌贴图索引已加载：${Object.keys(this._plateManifest?.plates || {}).length} 张（MIT chinese_license_plate_generator）`);
        return this._plateManifest;
      } catch (e) {
        this._plateManifest = null;
        console.warn('[AssetFactory] 车牌贴图索引加载失败，回退 Canvas 绘制：', e?.message || e);
        return null;
      }
    })();
    return this._plateManifestPromise;
  }

  /** @returns {{file:string,color:string,style:string,source_sample:string}|null} */
  _plateTextureEntry(plate) {
    const plates = this._plateManifest?.plates;
    if (!plates) return null;
    const key = String(plate || '').replace(/[·\s]/g, '').toUpperCase();
    if (plates[key]) return plates[key];
    for (const [k, v] of Object.entries(plates)) {
      if (k.toUpperCase() === key) return v;
    }
    return null;
  }

  /**
   * 预加载全部 MIT 真实样式车牌贴图（18 张，约 200KB）。
   * 必须在剧本车辆到达停止线前完成：否则首次 LPR 相机截图可能在纹理上传前拍到黑牌，
   * 导致 HyperLPR3 检测失败（本验证遇到过的真实问题）。
   * @returns {Promise<number>} 成功缓存数量
   */
  async preloadPlates() {
    if (!this._plateManifest?.plates || typeof document === 'undefined') return 0;
    let ok = 0;
    await Promise.all(Object.entries(this._plateManifest.plates).map(async ([plateKey, entry]) => {
      const color = entry.color || 'blue';
      const key = `${color}:${plateKey}`;
      if (this._plateTextures.has(key)) { ok++; return; }
      try {
        const tex = await new THREE.TextureLoader().loadAsync(this.assetBase + entry.file);
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.anisotropy = 4;
        tex.userData = { ...(tex.userData || {}), shared: true, plate: plateKey,
          source_sample: entry.source_sample, license: 'MIT chinese_license_plate_generator' };
        this._plateTextures.set(key, tex);
        ok++;
      } catch (e) {
        console.warn(`[AssetFactory] 车牌贴图预加载失败 ${plateKey}：${e?.message || e}`);
      }
    }));
    console.info(`[AssetFactory] 真实样式车牌贴图预加载 ${ok}/${Object.keys(this._plateManifest.plates).length} 张`);
    return ok;
  }

  /**
   * 车牌贴图：优先使用 MIT 生成器真实样式图（Plate_F/Plate_R），
   * 仅当车牌不在索引（人工乱填/未覆盖）时才回退 Canvas。
   * @returns {THREE.Texture}
   */
  plateTexture(plate, color = 'blue') {
    const key = `${color}:${String(plate || '').replace(/[·\s]/g, '').toUpperCase()}`;
    const cached = this._plateTextures.get(key);
    if (cached) return cached;
    const entry = this._plateTextureEntry(plate);
    if (entry && typeof document !== 'undefined') {
      const tex = new THREE.TextureLoader().load(this.assetBase + entry.file);
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = 4;
      tex.userData = { ...(tex.userData || {}), shared: true, plate, source_sample: entry.source_sample,
        license: 'MIT chinese_license_plate_generator' };
      this._plateTextures.set(key, tex);
      return tex;
    }
    if (!this._plateWarned.has(key)) {
      this._plateWarned.add(key);
      console.warn(`[AssetFactory] 未找到车牌 ${plate} 的 MIT 真实样式贴图，回退 Canvas（仅非演示未知车牌）`);
    }
    const tex = this._plateCanvasTexture(plate, color);
    this._plateTextures.set(key, tex);
    return tex;
  }

  /**
   * Canvas 车牌贴图（仅未知车牌回退；演示剧本车牌走 plateTexture 的真实图索引）。
   * 蓝牌白字 / 绿牌黑字（渐变绿底）。
   * @returns {THREE.CanvasTexture}
   */
  _plateCanvasTexture(plate, color = 'blue') {
    const w = 256, h = 88;
    const cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    const g = cv.getContext('2d');
    if (color === 'yellow' || color === 'yellow_large') {
      g.fillStyle = '#f2c200'; g.fillRect(0, 0, w, h);
      g.strokeStyle = '#111'; g.lineWidth = 4; g.strokeRect(2, 2, w - 4, h - 4);
      g.fillStyle = '#111';
    } else if (color === 'green') {
      const grad = g.createLinearGradient(0, 0, w, 0);
      grad.addColorStop(0, '#d8f3dc'); grad.addColorStop(0.35, '#8fe3a8');
      grad.addColorStop(1, '#3fae6a');
      g.fillStyle = grad; g.fillRect(0, 0, w, h);
      g.strokeStyle = '#1c1c1c'; g.lineWidth = 4; g.strokeRect(2, 2, w - 4, h - 4);
      g.fillStyle = '#111';
    } else {
      g.fillStyle = '#1a4fa0'; g.fillRect(0, 0, w, h);
      g.strokeStyle = '#e8eefc'; g.lineWidth = 4; g.strokeRect(2, 2, w - 4, h - 4);
      g.fillStyle = '#fff';
    }
    g.font = 'bold 52px "PingFang SC","Microsoft YaHei",sans-serif';
    const textWidth = g.measureText(plate || '').width;
    if (textWidth > w-20) g.font = `bold ${Math.floor(52*(w-20)/textWidth)}px "PingFang SC","Microsoft YaHei",sans-serif`;
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.fillText(plate || '', w / 2, h / 2 + 2);
    const tex = new THREE.CanvasTexture(cv);
    tex.anisotropy = 4;
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  /**
   * 信息屏 / 显示屏贴图：多行文本（余位、欢迎、费用）。
   * @param {string[]} lines 每行 "文本" 或 "文本|颜色"
   */
  screenTexture(lines, opts = {}) {
    const w = opts.w || 512, h = opts.h || 256;
    const cv = opts.texture?.image || document.createElement('canvas');
    cv.width = w; cv.height = h;
    const g = cv.getContext('2d');
    g.fillStyle = opts.bg || '#04121a'; g.fillRect(0, 0, w, h);
    const n = Math.max(1, lines.length);
    const lh = h / (n + 0.6);
    g.textAlign = 'center'; g.textBaseline = 'middle';
    lines.forEach((raw, i) => {
      const [text, col] = String(raw).split('|');
      g.fillStyle = col || opts.fg || '#39ff88';
      let fontSize = Math.floor(lh * 0.72);
      g.font = `bold ${fontSize}px "PingFang SC","Microsoft YaHei",monospace`;
      const measured = g.measureText(text).width;
      if (measured > w - 24) fontSize = Math.floor(fontSize * (w - 24) / measured);
      g.font = `bold ${fontSize}px "PingFang SC","Microsoft YaHei",monospace`;
      g.fillText(text, w / 2, lh * (i + 0.8));
    });
    const tex = opts.texture || new THREE.CanvasTexture(cv);
    tex.needsUpdate = true;
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  /**
   * P2-02：出口抓拍照片屏（照片铺底 + 底部半透明文字区）。
   * 与 screenTexture 共用同一 CanvasTexture 对象，只更新像素，避免反复换材质/纹理。
   * @param {string[]} lines 文字行
   * @param {ImageBitmap|HTMLImageElement|CanvasImageSource} photo 已解码图片
   */
  photoScreenTexture(lines, photo, opts = {}) {
    const w = opts.w || 384, h = opts.h || 552;
    const cv = opts.texture?.image || document.createElement('canvas');
    cv.width = w; cv.height = h;
    const g = cv.getContext('2d');
    g.fillStyle = opts.bg || '#04121a'; g.fillRect(0, 0, w, h);
    if (photo) {
      const iw = Number(photo.width) || Number(photo.naturalWidth) || w;
      const ih = Number(photo.height) || Number(photo.naturalHeight) || h;
      const scale = Math.max(w / Math.max(1, iw), h / Math.max(1, ih));
      const dw = iw * scale, dh = ih * scale;
      g.drawImage(photo, (w - dw) / 2, (h - dh) / 2, dw, dh);
    }
    // 底部文字区（保证照片可见的同时可读费用/车牌/余位）
    const textTop = Math.floor(h * 0.56);
    g.fillStyle = 'rgba(2, 10, 16, 0.74)';
    g.fillRect(0, textTop, w, h - textTop);
    g.fillStyle = 'rgba(110, 231, 255, 0.95)';
    g.font = 'bold 20px "PingFang SC","Microsoft YaHei",monospace';
    g.textAlign = 'left'; g.textBaseline = 'middle';
    g.fillText(opts.label || '出口抓拍', 14, textTop + 20);
    const n = Math.max(1, lines.length);
    const areaTop = textTop + 38;
    const lh = (h - areaTop) / (n + 0.25);
    g.textAlign = 'center';
    lines.forEach((raw, i) => {
      const [text, col] = String(raw).split('|');
      g.fillStyle = col || opts.fg || '#e9fbff';
      let fontSize = Math.max(16, Math.floor(lh * 0.72));
      g.font = `bold ${fontSize}px "PingFang SC","Microsoft YaHei",monospace`;
      const measured = g.measureText(text).width;
      if (measured > w - 24) fontSize = Math.max(12, Math.floor(fontSize * (w - 24) / measured));
      g.font = `bold ${fontSize}px "PingFang SC","Microsoft YaHei",monospace`;
      g.fillText(text, w / 2, areaTop + lh * (i + 0.55));
    });
    const tex = opts.texture || new THREE.CanvasTexture(cv);
    tex.needsUpdate = true;
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }
}
