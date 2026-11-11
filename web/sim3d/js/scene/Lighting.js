// 灯光与渲染器配置（scene/Lighting.js）—— route-v2 升级。
//
// 对照审查报告 §8.5.5 / assets_3d/renders/v2_* 的白天打光：
//   - 渲染器：ACESFilmicToneMapping + SRGBColorSpace 输出 + PCFSoftShadowMap；
//   - 太阳：暖白、低角度侧逆光（长阴影），shadow map 2048，正交阴影相机覆盖全场；
//   - 天光：半球光负责环境色，环境光/补光压低阴影死黑；
//   - 保留 setNight() 夜景模式（路灯材质可后续接入），并把雾/背景做成“场地尺度”。
import * as THREE from 'three';

export const RENDERER_DEFAULTS = {
  toneMappingExposure: 1.05,
  shadowMapSize: 2048,
  shadowRadius: 2.4,
  sunIntensity: 2.65,
  sunElevationDeg: 52,
  sunAzimuthDeg: 32,        // 相对 +X 的方位角（东南向侧光）
};

export class Lighting {
  constructor(scene, opts = {}) {
    this.scene = scene;
    this.opts = { ...RENDERER_DEFAULTS, ...opts };
    this.sunAzimuth = THREE.MathUtils.degToRad(this.opts.sunAzimuthDeg);
    this.sunElevation = THREE.MathUtils.degToRad(this.opts.sunElevationDeg);

    // 天光（半球光）：上方偏冷天光 + 地面暖反照
    this.hemi = new THREE.HemisphereLight(0xcfe0ff, 0x4b4636, 0.9);
    scene.add(this.hemi);

    // 环境光：只补阴影，不压平明暗
    this.ambient = new THREE.AmbientLight(0xffffff, 0.32);
    scene.add(this.ambient);

    // 太阳（主光，唯一投影光源）
    this.sun = new THREE.DirectionalLight(0xfff1dc, this.opts.sunIntensity);
    this._placeSun(70);
    this.sun.castShadow = true;
    const sm = this.opts.shadowMapSize;
    this.sun.shadow.mapSize.set(sm, sm);
    const d = 92;
    this.sun.shadow.camera.left = -d;
    this.sun.shadow.camera.right = d;
    this.sun.shadow.camera.top = d;
    this.sun.shadow.camera.bottom = -d;
    this.sun.shadow.camera.near = 1;
    this.sun.shadow.camera.far = 320;
    this.sun.shadow.bias = -0.00035;
    this.sun.shadow.normalBias = 0.02;
    this.sun.shadow.radius = this.opts.shadowRadius;
    scene.add(this.sun);
    scene.add(this.sun.target);

    // 补光（弱、无阴影）：从相机反方向补一点，避免背光面死黑
    this.fill = new THREE.DirectionalLight(0xcfe0ff, 0.42);
    this.fill.position.set(-60, 40, -50);
    scene.add(this.fill);

    // 颜色基调（与 shells/painted 场景统一的蓝灰天空）
    this.skyDay = new THREE.Color(0xa8c8ea);
    this.fogDay = new THREE.Color(0xa8c8ea);
    this.night = false;
    this.setNight(false);
  }

  /** 给 WebGLRenderer 应用统一色彩/阴影/色调映射（ParkingScene 构造时调用）。 */
  static configureRenderer(renderer) {
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = RENDERER_DEFAULTS.toneMappingExposure;
    if ('useLegacyLights' in renderer) renderer.useLegacyLights = false;
    return renderer;
  }

  _placeSun(distance = 70) {
    const el = this.sunElevation, az = this.sunAzimuth;
    const x = Math.cos(el) * Math.cos(az) * distance;
    const z = Math.cos(el) * Math.sin(az) * distance;
    const y = Math.sin(el) * distance;
    this.sun.position.set(x, y, z);
    this.sun.target.position.set(0, 0, 0);
    this.sun.target.updateMatrixWorld();
  }

  /** 让太阳阴影相机覆盖场地（manifest lot 尺寸变化时调用）。 */
  fitToLot(lot = null) {
    if (!lot) return;
    const w = Math.max(Math.abs(lot.x_min || -36), Math.abs(lot.x_max || 36));
    const h = Math.max(Math.abs(lot.y_min || -30), Math.abs(lot.y_max || 29.5));
    const d = Math.max(w, h) + 26;
    this.sun.shadow.camera.left = -d;
    this.sun.shadow.camera.right = d;
    this.sun.shadow.camera.top = d;
    this.sun.shadow.camera.bottom = -d;
    this.sun.shadow.camera.updateProjectionMatrix();
    const dist = Math.max(80, d * 1.1);
    this._placeSun(dist);
    this.sun.shadow.camera.far = dist * 3;
    this.sun.shadow.camera.updateProjectionMatrix();
  }

  setNight(on) {
    this.night = !!on;
    if (this.night) {
      this.sun.intensity = 0.22;
      this.sun.color.set(0x8fa6d8);
      this.hemi.intensity = 0.22;
      this.hemi.color.set(0x35507a);
      this.ambient.intensity = 0.10;
      this.fill.intensity = 0.12;
      this.scene.background = new THREE.Color(0x070b16);
      this.scene.fog = new THREE.Fog(0x070b16, 110, 330);
    } else {
      this.sun.intensity = this.opts.sunIntensity;
      this.sun.color.set(0xfff1dc);
      this.hemi.intensity = 0.9;
      this.hemi.color.set(0xcfe0ff);
      this.ambient.intensity = 0.32;
      this.fill.intensity = 0.42;
      this.scene.background = this.skyDay.clone();
      this.scene.fog = new THREE.Fog(this.fogDay, 190, 460);
    }
  }

  setExposure(v) {
    // 由 ParkingScene 持有 renderer；这里仅记录，供调试接口使用
    this.exposure = v;
  }
}
