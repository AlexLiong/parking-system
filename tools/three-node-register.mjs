// Resolve the same vendored Three.js module used by the browser import map.
import { registerHooks } from 'node:module';
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === 'three') return { shortCircuit: true,
    url: new URL('../web/sim3d/vendor/three.module.js', import.meta.url).href };
  if (specifier.startsWith('three/addons/')) return { shortCircuit: true,
    url: new URL('../web/sim3d/vendor/jsm/' + specifier.slice('three/addons/'.length), import.meta.url).href };
  return nextResolve(specifier, context);
} });
