import * as THREE from 'three';

// ── Config ────────────────────────────────────────────────────────────────────
const AID     = window.ASSESSMENT_ID;
const CAT     = window.CATEGORY_CODE;
const ACLASS  = (window.ANIMAL_CLASS || '').toLowerCase();
const HAS_IMG = window.HAS_IMAGE;
const N       = 2800;

function classToMode(cls) {
  if (cls.includes('aves'))                                        return 'flock';
  if (['actinopterygii','chondrichthyes','cephalaspidomorphi',
       'myxini','sarcopterygii'].some(c => cls.includes(c)))      return 'school';
  if (cls.includes('insecta') || cls.includes('arachnida') ||
      cls.includes('malacostraca'))                               return 'swarm';
  if (cls.includes('reptilia') || cls.includes('testudines') ||
      cls.includes('crocodylia') || cls.includes('squamata'))     return 'meander';
  if (cls.includes('amphibia'))                                   return 'pulse';
  return 'wander';
}

const MODE = classToMode(ACLASS);

// ── Renderer ──────────────────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x000000, 1);
document.getElementById('canvas-container').appendChild(renderer.domElement);

const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  55, window.innerWidth / window.innerHeight, 1, 2000
);
camera.position.z = 450;

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ── Mouse parallax ────────────────────────────────────────────────────────────
let mouseX = 0, mouseY = 0;
window.addEventListener('mousemove', e => {
  mouseX = (e.clientX / window.innerWidth  - 0.5) * 2;
  mouseY = (e.clientY / window.innerHeight - 0.5) * 2;
});

// ── Particle data fetch ───────────────────────────────────────────────────────
// Server returns {particles:[{x,y,z,r,g,b},...]} sampled from the illustration.
// x,y in [-1,1] (Three.js normalised), z in world units, r/g/b 0-255.
async function fetchParticleData() {
  try {
    const r = await fetch(`/api/particle-data/${AID}`);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

// ── CPU particle arrays ───────────────────────────────────────────────────────
const pos  = new Float32Array(N * 3);
const vel  = new Float32Array(N * 3);
const home = new Float32Array(N * 3);  // image-sampled home (x,y,z)
const ph   = new Float32Array(N);
const rlTh = new Float32Array(N);
const szM  = new Float32Array(N);

// GPU buffer arrays
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

const spriteTex = new THREE.TextureLoader().load(`/api/sprite-png/${AID}`);

const mat = new THREE.ShaderMaterial({
  uniforms: { uTex: { value: spriteTex } },
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
      gl_PointSize = aSize * (380.0 / -mv.z);
      gl_Position  = projectionMatrix * mv;
    }
  `,
  fragmentShader: `
    uniform sampler2D uTex;
    varying vec3  vColor;
    varying float vAlpha;
    void main() {
      vec4 t = texture2D(uTex, gl_PointCoord);
      if (t.a < 0.04) discard;
      // Particle colour from the sampled illustration pixel, boosted for additive blending
      vec3 col = vColor * 1.4;
      gl_FragColor = vec4(col, t.a * vAlpha);
    }
  `,
  blending:    THREE.AdditiveBlending,
  depthWrite:  false,
  transparent: true,
});

const points = new THREE.Points(geo, mat);
scene.add(points);

// ── State machine ─────────────────────────────────────────────────────────────
const STATES = ['scatter', 'gather', 'hold', 'alive'];
const DUR    = { scatter: 3.5, gather: 4.5, hold: 2.2, alive: 11 };
let state   = 'scatter';
let stTimer = 0;

function nextState() {
  state   = STATES[(STATES.indexOf(state) + 1) % STATES.length];
  stTimer = 0;
}

// ── Photo backdrop ────────────────────────────────────────────────────────────
const photoBg = document.getElementById('photo-bg');
if (HAS_IMG) photoBg.style.backgroundImage = `url('/api/source/${AID}')`;
function setPhoto(v) { photoBg.style.opacity = String(v); }

// ── Damping helper ────────────────────────────────────────────────────────────
function damp(factor, dt) { return Math.pow(factor, dt * 60); }

// ── Init ──────────────────────────────────────────────────────────────────────
function init(particles) {
  for (let i = 0; i < N; i++) {
    const p = particles[i % particles.length];

    // Home position from image-sampled data
    home[i*3]   = p.x * 300;          // [-1,1] → ±300 world units
    home[i*3+1] = p.y * 300;
    home[i*3+2] = p.z;                // 0–60 world units: darker pixels closer

    // Start scattered in a sphere
    const r = 300 + Math.random() * 200;
    const a = Math.random() * Math.PI * 2;
    const b = (Math.random() - 0.5) * Math.PI;
    pos[i*3]   = r * Math.cos(b) * Math.cos(a);
    pos[i*3+1] = r * Math.cos(b) * Math.sin(a);
    pos[i*3+2] = r * Math.sin(b) * 0.5;

    vel[i*3] = vel[i*3+1] = vel[i*3+2] = 0;

    ph[i]   = Math.random() * Math.PI * 2;
    rlTh[i] = Math.random();
    szM[i]  = 0.55 + Math.random() * 1.1;

    // Colour from illustration pixel
    gpuCol[i*3]   = p.r / 255;
    gpuCol[i*3+1] = p.g / 255;
    gpuCol[i*3+2] = p.b / 255;

    gpuSz[i]    = 9 * szM[i];
    gpuAlpha[i] = 0;
  }

  geo.attributes.aColor.needsUpdate = true;
  geo.attributes.aSize.needsUpdate  = true;
}

// ── Movement modes ────────────────────────────────────────────────────────────
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

  vel[i*3]   += ax * dt;
  vel[i*3+1] += ay * dt;
  vel[i*3+2] += az * dt;

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 90) { vel[i*3] *= 90/sp; vel[i*3+1] *= 90/sp; }
  vel[i*3]   *= damp(0.97, dt);
  vel[i*3+1] *= damp(0.97, dt);
  vel[i*3+2] *= damp(0.93, dt);
}

let schoolAng = 0;
function school(i, dt, t) {
  schoolAng = t * 0.25;
  const cx = Math.cos(schoolAng) * 100;
  const cy = Math.sin(schoolAng) * 60;
  const px = pos[i*3], py = pos[i*3+1];

  let ax = (cx - px) * 0.6;
  let ay = (cy - py) * 0.6;

  const perpX = -Math.sin(schoolAng);
  const perpY =  Math.cos(schoolAng);
  const wave  = Math.sin(t * 4 + ph[i]) * 20;
  ax += perpX * wave;
  ay += perpY * wave;

  vel[i*3]   += ax * dt;
  vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt * 0.6;

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 75) { vel[i*3] *= 75/sp; vel[i*3+1] *= 75/sp; }
  vel[i*3]   *= damp(0.96, dt);
  vel[i*3+1] *= damp(0.96, dt);
  vel[i*3+2] *= damp(0.90, dt);
}

function swarm(i, dt, t) {
  const qx = Math.sin(t * 0.45 + ph[i] * 0.4) * 80;
  const qy = Math.cos(t * 0.38 + ph[i] * 0.3) * 55;
  const px  = pos[i*3], py = pos[i*3+1];

  let ax = (qx - px) * 2.2;
  let ay = (qy - py) * 2.2;
  ax += Math.sin(t * 18 + ph[i] * 9) * 40;
  ay += Math.cos(t * 14 + ph[i] * 7) * 40;

  vel[i*3]   += ax * dt;
  vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt;

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 140) { vel[i*3] *= 140/sp; vel[i*3+1] *= 140/sp; }
  vel[i*3]   *= damp(0.86, dt);
  vel[i*3+1] *= damp(0.86, dt);
  vel[i*3+2] *= damp(0.84, dt);
}

function meander(i, dt, t) {
  const heading = ph[i] + t * 0.18;
  let ax = Math.cos(heading) * 18 - pos[i*3]   * 0.012;
  let ay = Math.sin(heading) * 18 - pos[i*3+1] * 0.012;

  vel[i*3]   += ax * dt;
  vel[i*3+1] += ay * dt;
  vel[i*3+2] *= damp(0.93, dt);

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 35) { vel[i*3] *= 35/sp; vel[i*3+1] *= 35/sp; }
  vel[i*3]   *= damp(0.985, dt);
  vel[i*3+1] *= damp(0.985, dt);
}

function pulse(i, dt, t) {
  const wave = Math.sin(t * 2.5 + ph[i]);
  const dist = Math.hypot(pos[i*3], pos[i*3+1]) || 1;
  const nx   = pos[i*3]   / dist;
  const ny   = pos[i*3+1] / dist;

  vel[i*3]   += nx * wave * 30 * dt;
  vel[i*3+1] += ny * wave * 30 * dt - pos[i*3+1] * 0.025 * dt * 60;
  vel[i*3+2] += Math.sin(t * 4 + ph[i]) * 8 * dt;

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 55) { vel[i*3] *= 55/sp; vel[i*3+1] *= 55/sp; }
  vel[i*3]   *= damp(0.965, dt);
  vel[i*3+1] *= damp(0.965, dt);
  vel[i*3+2] *= damp(0.91, dt);
}

function wander(i, dt, t) {
  const heading = ph[i] + Math.sin(t * 0.4 + ph[i] * 1.3) * 2.5;
  let ax = Math.cos(heading) * 28 - pos[i*3]   * 0.018;
  let ay = Math.sin(heading) * 28 - pos[i*3+1] * 0.018;

  if (Math.sin(t * 0.6 + ph[i] * 13) > 0.93) {
    ax += Math.cos(ph[i] * 7) * 160;
    ay += Math.sin(ph[i] * 7) * 160;
  }

  vel[i*3]   += ax * dt;
  vel[i*3+1] += ay * dt;
  vel[i*3+2] += (-pos[i*3+2]) * dt * 0.55;

  const sp = Math.hypot(vel[i*3], vel[i*3+1]);
  if (sp > 70) { vel[i*3] *= 70/sp; vel[i*3+1] *= 70/sp; }
  vel[i*3]   *= damp(0.972, dt);
  vel[i*3+1] *= damp(0.972, dt);
  vel[i*3+2] *= damp(0.91, dt);
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

  const prog = stTimer / DUR[state];

  if (HAS_IMG) {
    if      (state === 'scatter') setPhoto((Math.min(prog * 2, 1) * 0.65).toFixed(3));
    else if (state === 'gather')  setPhoto((Math.max(0, 1 - prog * 1.8) * 0.65).toFixed(3));
    else                          setPhoto('0');
  }

  const aliveRelease = state === 'alive' ? Math.min(1, stTimer / 3.5) : 0;

  camera.position.x += (mouseX * 35  - camera.position.x) * 0.03;
  camera.position.y += (-mouseY * 22 - camera.position.y) * 0.03;
  camera.lookAt(0, 0, 0);

  for (let i = 0; i < N; i++) {
    const ix = i*3, iy = i*3+1, iz = i*3+2;

    if (state === 'scatter') {
      vel[ix] += (Math.sin(t * 0.6 + ph[i]) * 8 - pos[ix] * 0.004) * dt * 60 * 0.012;
      vel[iy] += (Math.cos(t * 0.5 + ph[i]) * 8 - pos[iy] * 0.004) * dt * 60 * 0.012;
      vel[ix] *= damp(0.99, dt);
      vel[iy] *= damp(0.99, dt);
      vel[iz] *= damp(0.97, dt);

      const targetA = 0.45 + Math.sin(t * 2.2 + ph[i]) * 0.18;
      gpuAlpha[i] += (targetA - gpuAlpha[i]) * Math.min(dt * 2, 0.12);

    } else if (state === 'gather') {
      const k = 5.5, d = damp(0.88, dt);
      vel[ix] += (home[ix] - pos[ix]) * k * dt;
      vel[iy] += (home[iy] - pos[iy]) * k * dt;
      vel[iz] += (home[iz] - pos[iz]) * k * dt;
      vel[ix] *= d; vel[iy] *= d; vel[iz] *= d;

      gpuAlpha[i] += (0.82 - gpuAlpha[i]) * Math.min(dt * 1.8, 0.1);

    } else if (state === 'hold') {
      const bx = home[ix] + Math.sin(t * 2.1 + ph[i]) * 2.5;
      const by = home[iy] + Math.cos(t * 1.9 + ph[i]) * 2.5;
      const ks = 4.0, ds = damp(0.87, dt);
      vel[ix] += (bx - pos[ix]) * ks * dt;
      vel[iy] += (by - pos[iy]) * ks * dt;
      vel[iz] *= damp(0.92, dt);
      vel[ix] *= ds; vel[iy] *= ds;

      gpuAlpha[i] += (0.88 - gpuAlpha[i]) * Math.min(dt * 1.5, 0.08);

    } else { // alive
      if (aliveRelease > rlTh[i]) {
        moveFn(i, dt, t);
        const targetA = 0.55 + Math.sin(t * 3.5 + ph[i]) * 0.22;
        gpuAlpha[i] += (targetA - gpuAlpha[i]) * Math.min(dt * 1.2, 0.07);
      } else {
        const bx = home[ix] + Math.sin(t * 2.1 + ph[i]) * 2;
        const by = home[iy] + Math.cos(t * 1.9 + ph[i]) * 2;
        const ks = 4.0, ds = damp(0.87, dt);
        vel[ix] += (bx - pos[ix]) * ks * dt;
        vel[iy] += (by - pos[iy]) * ks * dt;
        vel[iz] *= damp(0.92, dt);
        vel[ix] *= ds; vel[iy] *= ds;
      }
    }

    pos[ix] += vel[ix] * dt;
    pos[iy] += vel[iy] * dt;
    pos[iz] += vel[iz] * dt;

    if      (pos[ix] >  420) pos[ix] = -420;
    else if (pos[ix] < -420) pos[ix] =  420;
    if      (pos[iy] >  360) pos[iy] = -360;
    else if (pos[iy] < -360) pos[iy] =  360;

    gpuPos[ix] = pos[ix];
    gpuPos[iy] = pos[iy];
    gpuPos[iz] = pos[iz];

    gpuSz[i] = szM[i] * (8.5 + Math.sin(t * 2.4 + ph[i]) * 2.2);
  }

  geo.attributes.position.needsUpdate = true;
  geo.attributes.aAlpha.needsUpdate   = true;
  geo.attributes.aSize.needsUpdate    = true;

  renderer.render(scene, camera);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  const data = await fetchParticleData();
  const particles = data?.particles?.length > 0
    ? data.particles
    : [{ x: 0, y: 0, z: 0, r: 160, g: 130, b: 100 }];

  init(particles);
  requestAnimationFrame(animate);
})();
