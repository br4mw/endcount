import * as THREE from 'three';

// ── Config ────────────────────────────────────────────────────────────────────
const AID        = window.ASSESSMENT_ID;
const CAT        = window.CATEGORY_CODE;
const ACLASS     = (window.ANIMAL_CLASS || '').toLowerCase();
const POPULATION = window.POPULATION;

const N = (CAT === 'EX' || CAT === 'EW')
  ? 10000
  : (POPULATION == null ? 2800 : Math.max(30, Math.min(POPULATION, 10000)));

// Particle size tuned for the 520px-tall embedded container
const BASE_SZ = Math.max(16, Math.min(56, 56 * Math.sqrt(2800 / N)));

function classToMode(cls) {
  if (cls.includes('aves'))                                           return 'flock';
  if (['actinopterygii','chondrichthyes','cephalaspidomorphi',
       'myxini','sarcopterygii'].some(c => cls.includes(c)))         return 'school';
  if (cls.includes('insecta') || cls.includes('arachnida') ||
      cls.includes('malacostraca'))                                   return 'swarm';
  if (cls.includes('reptilia') || cls.includes('testudines') ||
      cls.includes('crocodylia') || cls.includes('squamata'))        return 'meander';
  if (cls.includes('amphibia'))                                       return 'pulse';
  return 'wander';
}
const MODE = classToMode(ACLASS);

// ── Renderer ──────────────────────────────────────────────────────────────────
const container  = document.getElementById('art-container');
const canvasWrap = document.getElementById('art-canvas-wrap');
const artImg     = document.getElementById('species-art-img');

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000, 0);  // fully transparent background
canvasWrap.appendChild(renderer.domElement);

const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 2, 1, 2000);

function resize() {
  // Use the image element dimensions — the canvas must align exactly with
  // the displayed image, not the full container (which includes the meta strip).
  const imgH = artImg ? artImg.clientHeight : 520;
  const imgW = artImg ? artImg.clientWidth  : canvasWrap.clientWidth || container.clientWidth;

  renderer.setSize(imgW, imgH);
  camera.aspect = imgW / imgH;

  // Camera Z so that ±300 world units = exactly imgH pixels tall
  // (particle home positions are sampled from the image in [-1,1] → ×300)
  camera.position.z = 300 / Math.tan(27.5 * Math.PI / 180);  // ≈ 577
  camera.updateProjectionMatrix();
}
resize();

const resizeObs = new ResizeObserver(resize);
resizeObs.observe(container);

// ── CPU particle arrays ───────────────────────────────────────────────────────
const pos      = new Float32Array(N * 3);
const vel      = new Float32Array(N * 3);
const home     = new Float32Array(N * 3);
const ph       = new Float32Array(N);
const rlTh     = new Float32Array(N);
const szM      = new Float32Array(N);
const kickVel  = new Float32Array(N * 3);
const launched = new Uint8Array(N);

const gpuPos   = new Float32Array(N * 3);
const gpuCol   = new Float32Array(N * 3);
const gpuSz    = new Float32Array(N);
const gpuAlpha = new Float32Array(N);

// ── Geometry + shader ─────────────────────────────────────────────────────────
const geo = new THREE.BufferGeometry();
geo.setAttribute('position', new THREE.BufferAttribute(gpuPos,   3).setUsage(THREE.DynamicDrawUsage));
geo.setAttribute('aColor',   new THREE.BufferAttribute(gpuCol,   3));
geo.setAttribute('aSize',    new THREE.BufferAttribute(gpuSz,    1).setUsage(THREE.DynamicDrawUsage));
geo.setAttribute('aAlpha',   new THREE.BufferAttribute(gpuAlpha, 1).setUsage(THREE.DynamicDrawUsage));

// Sprite or gaussian-disk fallback.
// uSprite / uUseSprite: set after async texture load.
// uBright: 1.0 in light mode, boosted in dark mode so additive blending is visible.
const mat = new THREE.ShaderMaterial({
  uniforms: {
    uSprite:    { value: null },
    uUseSprite: { value: 0.0 },
    uBright:    { value: 1.0 },
  },
  vertexShader: `
    attribute float aSize;
    attribute vec3  aColor;
    attribute float aAlpha;
    varying   vec3  vColor;
    varying   float vAlpha;
    void main() {
      vColor = aColor;
      vAlpha = aAlpha;
      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      gl_PointSize = aSize * (500.0 / -mv.z);
      gl_Position  = projectionMatrix * mv;
    }
  `,
  fragmentShader: `
    uniform sampler2D uSprite;
    uniform float     uUseSprite;
    uniform float     uBright;
    varying vec3      vColor;
    varying float     vAlpha;
    void main() {
      float mask;
      if (uUseSprite > 0.5) {
        mask = texture2D(uSprite, gl_PointCoord).a;
      } else {
        vec2  uv = gl_PointCoord - 0.5;
        float d  = dot(uv, uv) * 4.0;
        mask = exp(-d * 5.0);
      }
      if (mask < 0.02) discard;
      gl_FragColor = vec4(vColor * uBright, mask * vAlpha);
    }
  `,
  blending:    THREE.NormalBlending,
  depthWrite:  false,
  transparent: true,
});

const points = new THREE.Points(geo, mat);
scene.add(points);

// ── Theme switch (called by toggle button) ────────────────────────────────────
window.setHeroDark = function(dark) {
  mat.blending = dark ? THREE.AdditiveBlending : THREE.NormalBlending;
  mat.uniforms.uBright.value = dark ? 3.0 : 1.0;
  mat.needsUpdate = true;
};

// Let the toggle script know the material is ready
document.dispatchEvent(new CustomEvent('particlesReady'));

// ── State machine ─────────────────────────────────────────────────────────────
// show    (4s) : image=1.0, particles hidden at home
// fadeout (2s) : image 1→0, particles fade in at home
// scatter (10s): particles launch from animal shape and animate freely
// [extinct] gone (2s) : total darkness — particles drift invisibly
// gather  (5s) : particles return to home positions
// fadein  (5s) : particles fade out, image 0→1
// [extinct] linger (4s) : image=1.0, particles dissolve to nothing
const IS_EXTINCT = POPULATION === 0;
const STATES = IS_EXTINCT
  ? ['show', 'fadeout', 'scatter', 'gone', 'gather', 'fadein', 'linger']
  : ['show', 'fadeout', 'scatter', 'gather', 'fadein'];
const DUR    = { show: 4, fadeout: 2, scatter: 10, gather: 5, fadein: 5, linger: 4, gone: 2 };

let state   = 'show';
let stTimer = 0;

function nextState() {
  state   = STATES[(STATES.indexOf(state) + 1) % STATES.length];
  stTimer = 0;
  if (state === 'scatter') launched.fill(0);
}

function setImgOpacity(v) {
  if (artImg) artImg.style.opacity = Math.max(0, Math.min(1, v)).toFixed(4);
}

// ── Particle data ─────────────────────────────────────────────────────────────
async function fetchParticleData() {
  try {
    const r = await fetch(`/api/particle-data/${AID}`);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function loadSpriteTexture() {
  return new Promise(resolve => {
    new THREE.TextureLoader().load(
      `/api/sprite-png/${AID}`,
      tex => { tex.minFilter = THREE.LinearFilter; resolve(tex); },
      undefined,
      () => resolve(null)
    );
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
// Jitter breaks the regular sampling grid so gathered particles look organic
const HOME_JITTER = 5.5;

function init(particles) {
  for (let i = 0; i < N; i++) {
    const p = particles[i % particles.length];

    // Jitter home positions to prevent the regular grid from becoming visible.
    // Scale is 272 rather than 300: object-fit:contain centres a square image
    // in the wider container, and the camera frustum covers the full element
    // width — the 9% reduction aligns the particle cluster with the actual
    // displayed image region.
    home[i*3]   = p.x * 272 + (Math.random() - 0.5) * HOME_JITTER;
    home[i*3+1] = p.y * 272 + (Math.random() - 0.5) * HOME_JITTER;
    home[i*3+2] = p.z;

    // Start at home — image is shown first, particles hidden
    pos[i*3]   = home[i*3];
    pos[i*3+1] = home[i*3+1];
    pos[i*3+2] = home[i*3+2];
    vel[i*3] = vel[i*3+1] = vel[i*3+2] = 0;

    ph[i]   = Math.random() * Math.PI * 2;
    rlTh[i] = Math.random();
    szM[i]  = 0.6 + Math.random() * 0.85;

    const kSpeed = 50 + Math.random() * 130;
    const kA     = Math.random() * Math.PI * 2;
    const kB     = (Math.random() - 0.5) * Math.PI;
    kickVel[i*3]   =  kSpeed * Math.cos(kB) * Math.cos(kA);
    kickVel[i*3+1] = -kSpeed * Math.cos(kB) * Math.sin(kA);  // reversed Y scatter
    kickVel[i*3+2] =  kSpeed * Math.sin(kB) * 0.3;

    gpuCol[i*3]   = p.r / 255;
    gpuCol[i*3+1] = p.g / 255;
    gpuCol[i*3+2] = p.b / 255;

    gpuSz[i]    = BASE_SZ * szM[i];
    gpuAlpha[i] = 0;
  }

  geo.attributes.aColor.needsUpdate = true;
  geo.attributes.aSize.needsUpdate  = true;
}

// ── Movement modes ────────────────────────────────────────────────────────────
function damp(factor, dt) { return Math.pow(factor, dt * 60); }

const flockCX = { x: 0, y: 0, z: 0 };
function flock(i, dt, t) {
  flockCX.x = Math.sin(t * 0.22) * 120;
  flockCX.y = Math.sin(t * 0.40) * 70;
  flockCX.z = Math.sin(t * 0.11) * 25;
  const px = pos[i*3], py = pos[i*3+1], pz = pos[i*3+2];
  let ax = (flockCX.x - px) * 0.9;
  let ay = (flockCX.y - py) * 0.9;
  let az = (flockCX.z - pz) * 0.6;
  const d2 = ax*ax + ay*ay;
  if (d2 < 35*35) { ax *= -2.5; ay *= -2.5; }
  ay += Math.sin(t * 9 + ph[i]) * 18;
  ax += Math.cos(t * 0.28) * 22;
  ay += Math.sin(t * 0.17) * 12;
  vel[i*3]   += ax * dt; vel[i*3+1] += ay * dt; vel[i*3+2] += az * dt;
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 90) { vel[i*3] *= 90/sp; vel[i*3+1] *= 90/sp; }
  vel[i*3]   *= damp(0.97, dt); vel[i*3+1] *= damp(0.97, dt); vel[i*3+2] *= damp(0.93, dt);
}

let schoolAng = 0;
function school(i, dt, t) {
  schoolAng = t * 0.25;
  const cx = Math.cos(schoolAng) * 100;
  const cy = Math.sin(schoolAng) * 60;
  const px = pos[i*3], py = pos[i*3+1];
  let ax = (cx - px) * 0.6;
  let ay = (cy - py) * 0.6;
  const perpX = -Math.sin(schoolAng), perpY = Math.cos(schoolAng);
  const wave  = Math.sin(t * 4 + ph[i]) * 20;
  ax += perpX * wave; ay += perpY * wave;
  vel[i*3]   += ax * dt; vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt * 0.6;
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 75) { vel[i*3] *= 75/sp; vel[i*3+1] *= 75/sp; }
  vel[i*3] *= damp(0.96, dt); vel[i*3+1] *= damp(0.96, dt); vel[i*3+2] *= damp(0.90, dt);
}

function swarm(i, dt, t) {
  const qx = Math.sin(t * 0.45 + ph[i] * 0.4) * 80;
  const qy = Math.cos(t * 0.38 + ph[i] * 0.3) * 55;
  const px  = pos[i*3], py = pos[i*3+1];
  let ax = (qx - px) * 2.2;
  let ay = (qy - py) * 2.2;
  ax += Math.sin(t * 18 + ph[i] * 9) * 40;
  ay += Math.cos(t * 14 + ph[i] * 7) * 40;
  vel[i*3]   += ax * dt; vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt;
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 140) { vel[i*3] *= 140/sp; vel[i*3+1] *= 140/sp; }
  vel[i*3] *= damp(0.86, dt); vel[i*3+1] *= damp(0.86, dt); vel[i*3+2] *= damp(0.84, dt);
}

function meander(i, dt, t) {
  const heading = ph[i] + t * 0.18;
  let ax = Math.cos(heading) * 18 - pos[i*3]   * 0.012;
  let ay = Math.sin(heading) * 18 - pos[i*3+1] * 0.012;
  vel[i*3]   += ax * dt; vel[i*3+1] += ay * dt;
  vel[i*3+2] *= damp(0.93, dt);
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 35) { vel[i*3] *= 35/sp; vel[i*3+1] *= 35/sp; }
  vel[i*3] *= damp(0.985, dt); vel[i*3+1] *= damp(0.985, dt);
}

function pulse(i, dt, t) {
  const wave = Math.sin(t * 2.5 + ph[i]);
  const dist = Math.hypot(pos[i*3], pos[i*3+1]) || 1;
  vel[i*3]   += (pos[i*3]   / dist) * wave * 30 * dt;
  vel[i*3+1] += (pos[i*3+1] / dist) * wave * 30 * dt - pos[i*3+1] * 0.025 * dt * 60;
  vel[i*3+2] += Math.sin(t * 4 + ph[i]) * 8 * dt;
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 55) { vel[i*3] *= 55/sp; vel[i*3+1] *= 55/sp; }
  vel[i*3] *= damp(0.965, dt); vel[i*3+1] *= damp(0.965, dt); vel[i*3+2] *= damp(0.91, dt);
}

function wander(i, dt, t) {
  const heading = ph[i] + Math.sin(t * 0.4 + ph[i] * 1.3) * 2.5;
  let ax = Math.cos(heading) * 28 - pos[i*3]   * 0.018;
  let ay = Math.sin(heading) * 28 - pos[i*3+1] * 0.018;
  if (Math.sin(t * 0.6 + ph[i] * 13) > 0.93) {
    ax += Math.cos(ph[i] * 7) * 160; ay += Math.sin(ph[i] * 7) * 160;
  }
  vel[i*3]   += ax * dt; vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt * 0.55;
  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 70) { vel[i*3] *= 70/sp; vel[i*3+1] *= 70/sp; }
  vel[i*3] *= damp(0.972, dt); vel[i*3+1] *= damp(0.972, dt); vel[i*3+2] *= damp(0.91, dt);
}

const MOVE   = { flock, school, swarm, meander, pulse, wander };
const moveFn = MOVE[MODE] || wander;

// ── Main loop ─────────────────────────────────────────────────────────────────
let lastT = 0;

function animate(ms) {
  requestAnimationFrame(animate);

  const t  = ms / 1000;
  const dt = Math.min(t - lastT, 0.05);
  lastT = t;

  stTimer += dt;
  if (stTimer > DUR[state]) nextState();

  const prog = Math.min(stTimer / DUR[state], 1);

  // Manage image opacity
  if      (state === 'show')    setImgOpacity(1);
  else if (state === 'fadeout') setImgOpacity(1 - prog);
  else if (state === 'scatter') setImgOpacity(0);
  else if (state === 'gather')  setImgOpacity(0);
  else if (state === 'fadein')  setImgOpacity(prog);
  else if (state === 'linger')  setImgOpacity(1);
  else if (state === 'gone')    setImgOpacity(0);

  const scatterRelease = state === 'scatter' ? Math.min(1, stTimer / 3.5) : 0;

  for (let i = 0; i < N; i++) {
    const ix = i*3, iy = i*3+1, iz = i*3+2;

    if (state === 'show') {
      // Image is visible; particles silently wait at home
      pos[ix] = home[ix]; pos[iy] = home[iy]; pos[iz] = home[iz];
      vel[ix] = vel[iy] = vel[iz] = 0;
      gpuAlpha[i] = 0;

    } else if (state === 'fadeout') {
      // Particles fade in at home with gentle breathing
      const bx = home[ix] + Math.sin(t * 2.0 + ph[i]) * 1.8;
      const by = home[iy] + Math.cos(t * 1.7 + ph[i]) * 1.8;
      pos[ix] = bx; pos[iy] = by; pos[iz] = home[iz];
      vel[ix] = vel[iy] = vel[iz] = 0;
      gpuAlpha[i] += (prog * 0.92 - gpuAlpha[i]) * Math.min(dt * 4, 0.18);

    } else if (state === 'scatter') {
      // Wave-release: particles launch outward from animal shape
      if (scatterRelease > rlTh[i]) {
        if (!launched[i]) {
          vel[ix] = kickVel[i*3];
          vel[iy] = kickVel[i*3+1];
          vel[iz] = kickVel[i*3+2];
          launched[i] = 1;
        }
        moveFn(i, dt, t);
        const tgt = 0.82 + Math.sin(t * 3.2 + ph[i]) * 0.1;
        gpuAlpha[i] += (tgt - gpuAlpha[i]) * Math.min(dt * 1.2, 0.07);
      } else {
        // Not yet released — breathe at home
        const bx = home[ix] + Math.sin(t * 2.0 + ph[i]) * 2;
        const by = home[iy] + Math.cos(t * 1.7 + ph[i]) * 2;
        vel[ix] += (bx - pos[ix]) * 4 * dt;
        vel[iy] += (by - pos[iy]) * 4 * dt;
        vel[ix] *= damp(0.88, dt); vel[iy] *= damp(0.88, dt);
        gpuAlpha[i] += (0.88 - gpuAlpha[i]) * Math.min(dt * 2, 0.1);
      }
      pos[ix] += vel[ix] * dt; pos[iy] += vel[iy] * dt; pos[iz] += vel[iz] * dt;

    } else if (state === 'gather') {
      // Spring toward a breathing target so particles never freeze onto exact grid positions
      const bx = home[ix] + Math.sin(t * 1.8 + ph[i]) * 4.5;
      const by = home[iy] + Math.cos(t * 1.5 + ph[i]) * 4.5;
      const k = 5.5, d = damp(0.88, dt);
      vel[ix] += (bx - pos[ix]) * k * dt;
      vel[iy] += (by - pos[iy]) * k * dt;
      vel[iz] += (home[iz] - pos[iz]) * k * dt;
      vel[ix] *= d; vel[iy] *= d; vel[iz] *= d;
      pos[ix] += vel[ix] * dt; pos[iy] += vel[iy] * dt; pos[iz] += vel[iz] * dt;
      gpuAlpha[i] += (0.88 - gpuAlpha[i]) * Math.min(dt * 1.8, 0.1);

    } else if (state === 'fadein') {
      // Continuous organic drift near home while image fades back in.
      // For extinct species, stop fading at 0.30 so linger can carry the rest.
      const bx = home[ix] + Math.sin(t * 1.8 + ph[i]) * 4.5;
      const by = home[iy] + Math.cos(t * 1.5 + ph[i]) * 4.5;
      const ks = 3.5, ds = damp(0.90, dt);
      vel[ix] += (bx - pos[ix]) * ks * dt;
      vel[iy] += (by - pos[iy]) * ks * dt;
      vel[ix] *= ds; vel[iy] *= ds;
      pos[ix] += vel[ix] * dt; pos[iy] += vel[iy] * dt; pos[iz] = home[iz];
      const tgt = IS_EXTINCT ? (0.30 + (1 - prog) * 0.58) : (1 - prog) * 0.88;
      gpuAlpha[i] += (tgt - gpuAlpha[i]) * Math.min(dt * 2.5, 0.14);

    } else if (state === 'linger') {
      // Image is fully back; particles dissolve slowly — the last of the animal fading.
      const bx = home[ix] + Math.sin(t * 1.8 + ph[i]) * 4.5;
      const by = home[iy] + Math.cos(t * 1.5 + ph[i]) * 4.5;
      const ks = 3.5, ds = damp(0.90, dt);
      vel[ix] += (bx - pos[ix]) * ks * dt;
      vel[iy] += (by - pos[iy]) * ks * dt;
      vel[ix] *= ds; vel[iy] *= ds;
      pos[ix] += vel[ix] * dt; pos[iy] += vel[iy] * dt; pos[iz] = home[iz];
      const tgt = (1 - prog) * 0.30;
      gpuAlpha[i] += (tgt - gpuAlpha[i]) * Math.min(dt * 1.5, 0.08);

    } else if (state === 'gone') {
      // Total darkness — particles drift invisibly so gather can pull them
      // back from spread-out positions rather than snapping from home.
      moveFn(i, dt, t);
      pos[ix] += vel[ix] * dt; pos[iy] += vel[iy] * dt; pos[iz] += vel[iz] * dt;
      gpuAlpha[i] = 0;
    }

    // Wrap bounds during scatter and the invisible gone drift
    if (state === 'scatter' || state === 'gone') {
      if      (pos[ix] >  420) pos[ix] = -420;
      else if (pos[ix] < -420) pos[ix] =  420;
      if      (pos[iy] >  360) pos[iy] = -360;
      else if (pos[iy] < -360) pos[iy] =  360;
    }

    gpuPos[ix] = pos[ix]; gpuPos[iy] = pos[iy]; gpuPos[iz] = pos[iz];
    gpuSz[i] = szM[i] * (BASE_SZ + Math.sin(t * 2.4 + ph[i]) * BASE_SZ * 0.12);
  }

  geo.attributes.position.needsUpdate = true;
  geo.attributes.aAlpha.needsUpdate   = true;
  geo.attributes.aSize.needsUpdate    = true;

  renderer.render(scene, camera);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  const [data, spriteTex] = await Promise.all([fetchParticleData(), loadSpriteTexture()]);

  if (spriteTex) {
    mat.uniforms.uSprite.value    = spriteTex;
    mat.uniforms.uUseSprite.value = 1.0;
  }

  const particles = data?.particles?.length > 0
    ? data.particles
    : [{ x: 0, y: 0, z: 0, r: 80, g: 60, b: 40 }];

  init(particles);
  requestAnimationFrame(animate);
})();
