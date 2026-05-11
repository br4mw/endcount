import * as THREE from 'three';

// ── Config ────────────────────────────────────────────────────────────────────
const AID      = window.ASSESSMENT_ID;
const CAT      = window.CATEGORY_CODE;
const ACLASS   = (window.ANIMAL_CLASS || '').toLowerCase();
const HAS_IMG  = window.HAS_IMAGE;

// Bright emissive colours for additive blending on black
const CAT_COLORS = {
  EX: '#D4926A',  // amber ember — extinct
  EW: '#B88FCC',  // soft violet — gone from wild
  CR: '#E04830',  // deep red
  EN: '#E87A38',  // burnt orange
  VU: '#D4B840',  // golden
  NT: '#78BA58',  // fresh green
  LC: '#48A498',  // teal
};
const BASE_HEX = CAT_COLORS[CAT] || '#B0A090';

// Movement mode from animal class
function classToMode(cls) {
  if (cls.includes('aves'))                                        return 'flock';
  if (['actinopterygii','chondrichthyes','cephalaspidomorphi',
       'myxini','sarcopterygii'].some(c => cls.includes(c)))      return 'school';
  if (cls.includes('insecta') || cls.includes('arachnida') ||
      cls.includes('malacostraca'))                               return 'swarm';
  if (cls.includes('reptilia') || cls.includes('testudines') ||
      cls.includes('crocodylia') || cls.includes('squamata'))     return 'meander';
  if (cls.includes('amphibia'))                                   return 'pulse';
  return 'wander'; // mammals, plants, fungi, unknowns
}

const MODE = classToMode(ACLASS);
const N    = 2800; // particle count

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

// ── Silhouette sampling ───────────────────────────────────────────────────────
// Samples the sprite PNG (white-on-transparent) to find home positions for the
// particle silhouette formation. Runs once at startup.
function sampleSilhouette() {
  return new Promise(resolve => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      const c   = document.createElement('canvas');
      c.width   = img.width;
      c.height  = img.height;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const px  = ctx.getImageData(0, 0, img.width, img.height).data;
      const pts = [];
      // Step size chosen so we get roughly N/2 sample points
      const step = Math.max(2, Math.round(Math.sqrt((img.width * img.height) / (N * 0.6))));
      for (let y = 0; y < img.height; y += step) {
        for (let x = 0; x < img.width; x += step) {
          if (px[(y * img.width + x) * 4 + 3] > 80) {
            pts.push([
              (x / img.width  - 0.5) * 300,
              -(y / img.height - 0.5) * 300,
            ]);
          }
        }
      }
      resolve(pts.length > 10 ? pts : [[0, 0]]);
    };
    img.onerror = () => resolve([[0, 0]]);
    img.src = `/api/sprite-png/${AID}`;
  });
}

// ── CPU particle arrays ───────────────────────────────────────────────────────
const pos  = new Float32Array(N * 3);  // current world position
const vel  = new Float32Array(N * 3);  // velocity (pixels/sec)
const home = new Float32Array(N * 3);  // silhouette target
const ph   = new Float32Array(N);      // phase offset for oscillations
const rlTh = new Float32Array(N);      // staggered 'alive' release threshold [0,1]
const szM  = new Float32Array(N);      // size multiplier per particle

// GPU buffer arrays (written each frame)
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
      // brighten the centre of each particle for a glowing core
      float core = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
      gl_FragColor = vec4(vColor * (t.rgb * 1.4 + core * 0.5), t.a * vAlpha);
    }
  `,
  blending:    THREE.AdditiveBlending,
  depthWrite:  false,
  transparent: true,
});

const points = new THREE.Points(geo, mat);
scene.add(points);

// ── State machine ─────────────────────────────────────────────────────────────
// scatter → gather → hold → alive → scatter → ...
const STATES = ['scatter', 'gather', 'hold', 'alive'];
const DUR    = { scatter: 3.5, gather: 4.5, hold: 2.2, alive: 11 };
let state     = 'scatter';
let stTimer   = 0;

function nextState() {
  state   = STATES[(STATES.indexOf(state) + 1) % STATES.length];
  stTimer = 0;
}

// ── Photo backdrop ────────────────────────────────────────────────────────────
const photoBg = document.getElementById('photo-bg');
if (HAS_IMG) photoBg.style.backgroundImage = `url('/api/source/${AID}')`;

function setPhoto(v) { photoBg.style.opacity = String(v); }

// ── Damping helper ────────────────────────────────────────────────────────────
// Frame-rate-independent multiplicative decay
function damp(factor, dt) { return Math.pow(factor, dt * 60); }

// ── Init particles ────────────────────────────────────────────────────────────
function init(silPts) {
  const base = new THREE.Color(BASE_HEX);
  const hsl  = {};
  base.getHSL(hsl);

  for (let i = 0; i < N; i++) {
    // Scatter positions — distributed in a sphere
    const r = 300 + Math.random() * 200;
    const a = Math.random() * Math.PI * 2;
    const b = (Math.random() - 0.5) * Math.PI;
    pos[i*3]   = r * Math.cos(b) * Math.cos(a);
    pos[i*3+1] = r * Math.cos(b) * Math.sin(a);
    pos[i*3+2] = r * Math.sin(b) * 0.5;

    vel[i*3] = vel[i*3+1] = vel[i*3+2] = 0;

    // Silhouette home
    const pt    = silPts[i % silPts.length];
    home[i*3]   = pt[0];
    home[i*3+1] = pt[1];
    home[i*3+2] = (Math.random() - 0.5) * 8;

    ph[i]   = Math.random() * Math.PI * 2;
    rlTh[i] = Math.random();  // staggered alive release
    szM[i]  = 0.55 + Math.random() * 1.1;

    // Per-particle colour: hue ± 0.1, saturation and lightness variation
    const c = new THREE.Color();
    c.setHSL(
      ((hsl.h + (Math.random() - 0.5) * 0.12) + 1) % 1,
      Math.min(1, hsl.s * (0.6 + Math.random() * 0.7)),
      Math.min(1, hsl.l * (0.5 + Math.random() * 1.1)),
    );
    gpuCol[i*3] = c.r; gpuCol[i*3+1] = c.g; gpuCol[i*3+2] = c.b;

    gpuSz[i]    = 9 * szM[i];
    gpuAlpha[i] = 0;
  }

  geo.attributes.aColor.needsUpdate = true;
  geo.attributes.aSize.needsUpdate  = true;
}

// ── Movement modes ────────────────────────────────────────────────────────────
// Each function modifies vel[i*3..i*3+2].
// Velocity is in pixels/second; caller advances pos by vel * dt.

const flockCX = { x: 0, y: 0, z: 0 }; // lazy flock centre

function flock(i, dt, t) {
  // Slow figure-eight flock centre
  flockCX.x = Math.sin(t * 0.22) * 120;
  flockCX.y = Math.sin(t * 0.40) * 70;
  flockCX.z = Math.sin(t * 0.11) * 25;

  const px = pos[i*3], py = pos[i*3+1], pz = pos[i*3+2];

  // Cohesion — pull toward flock centre
  let ax = (flockCX.x - px) * 0.9;
  let ay = (flockCX.y - py) * 0.9;
  let az = (flockCX.z - pz) * 0.6;

  // Soft separation — push away from centre if very close
  const d2 = ax*ax + ay*ay;
  if (d2 < 35*35) { ax *= -2.5; ay *= -2.5; }

  // Wingbeat — vertical sine oscillation
  ay += Math.sin(t * 9 + ph[i]) * 18;

  // Rotating wind
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
  schoolAng   = t * 0.25;
  const cx    = Math.cos(schoolAng) * 100;
  const cy    = Math.sin(schoolAng) * 60;
  const px    = pos[i*3], py = pos[i*3+1];

  // Follow oval current
  let ax = (cx - px) * 0.6;
  let ay = (cy - py) * 0.6;

  // Body undulation perpendicular to motion
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
  // Each particle orbits a slowly drifting queen point
  const qx = Math.sin(t * 0.45 + ph[i] * 0.4) * 80;
  const qy = Math.cos(t * 0.38 + ph[i] * 0.3) * 55;
  const px  = pos[i*3], py = pos[i*3+1];

  let ax = (qx - px) * 2.2;
  let ay = (qy - py) * 2.2;

  // Rapid chaotic flutter
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
  // Slow sinusoidal heading that rotates very gradually
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
  // Amphibian: radial pulse waves emanating from centre
  const wave  = Math.sin(t * 2.5 + ph[i]);
  const dist  = Math.hypot(pos[i*3], pos[i*3+1]) || 1;
  const nx    = pos[i*3]   / dist;
  const ny    = pos[i*3+1] / dist;

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
  // Mammal: heading drifts via low-frequency sine; centre gravity
  const heading = ph[i] + Math.sin(t * 0.4 + ph[i] * 1.3) * 2.5;
  let ax = Math.cos(heading) * 28 - pos[i*3]   * 0.018;
  let ay = Math.sin(heading) * 28 - pos[i*3+1] * 0.018;

  // Rare startles (sharp non-random bursts based on phase)
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

const MOVE = { flock, school, swarm, meander, pulse, wander };
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

  const prog = stTimer / DUR[state]; // 0→1 within current state

  // Photo opacity: show during scatter, fade before gather completes
  if (HAS_IMG) {
    if      (state === 'scatter') setPhoto((Math.min(prog * 2, 1) * 0.65).toFixed(3));
    else if (state === 'gather')  setPhoto((Math.max(0, 1 - prog * 1.8) * 0.65).toFixed(3));
    else                          setPhoto('0');
  }

  // Alive-state stagger: what fraction of particles are released
  const aliveRelease = state === 'alive' ? Math.min(1, stTimer / 3.5) : 0;

  // Camera parallax
  camera.position.x += (mouseX * 35  - camera.position.x) * 0.03;
  camera.position.y += (-mouseY * 22 - camera.position.y) * 0.03;
  camera.lookAt(0, 0, 0);

  for (let i = 0; i < N; i++) {
    const ix = i*3, iy = i*3+1, iz = i*3+2;

    // ── Per-state physics ─────────────────────────────────────────────────────
    if (state === 'scatter') {
      // Gentle drift outward with slow decay toward rest
      vel[ix] += (Math.sin(t * 0.6 + ph[i]) * 8 - pos[ix] * 0.004) * dt * 60 * 0.012;
      vel[iy] += (Math.cos(t * 0.5 + ph[i]) * 8 - pos[iy] * 0.004) * dt * 60 * 0.012;
      vel[ix] *= damp(0.99, dt);
      vel[iy] *= damp(0.99, dt);
      vel[iz] *= damp(0.97, dt);

      const targetA = 0.45 + Math.sin(t * 2.2 + ph[i]) * 0.18;
      gpuAlpha[i] += (targetA - gpuAlpha[i]) * Math.min(dt * 2, 0.12);

    } else if (state === 'gather') {
      // Spring toward silhouette home
      const k = 5.5, d = damp(0.88, dt);
      vel[ix] += (home[ix] - pos[ix]) * k * dt;
      vel[iy] += (home[iy] - pos[iy]) * k * dt;
      vel[iz] += (home[iz] - pos[iz]) * k * dt;
      vel[ix] *= d; vel[iy] *= d; vel[iz] *= d;

      gpuAlpha[i] += (0.82 - gpuAlpha[i]) * Math.min(dt * 1.8, 0.1);

    } else if (state === 'hold') {
      // Silhouette holds with soft breathing oscillation
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
        // Released — creature movement
        moveFn(i, dt, t);
        const targetA = 0.55 + Math.sin(t * 3.5 + ph[i]) * 0.22;
        gpuAlpha[i] += (targetA - gpuAlpha[i]) * Math.min(dt * 1.2, 0.07);
      } else {
        // Still holding formation
        const bx = home[ix] + Math.sin(t * 2.1 + ph[i]) * 2;
        const by = home[iy] + Math.cos(t * 1.9 + ph[i]) * 2;
        const ks = 4.0, ds = damp(0.87, dt);
        vel[ix] += (bx - pos[ix]) * ks * dt;
        vel[iy] += (by - pos[iy]) * ks * dt;
        vel[iz] *= damp(0.92, dt);
        vel[ix] *= ds; vel[iy] *= ds;
      }
    }

    // ── Integrate position ────────────────────────────────────────────────────
    pos[ix] += vel[ix] * dt;
    pos[iy] += vel[iy] * dt;
    pos[iz] += vel[iz] * dt;

    // Soft wrap — teleport to opposite edge
    if      (pos[ix] >  420) pos[ix] = -420;
    else if (pos[ix] < -420) pos[ix] =  420;
    if      (pos[iy] >  360) pos[iy] = -360;
    else if (pos[iy] < -360) pos[iy] =  360;

    // ── Write to GPU buffers ──────────────────────────────────────────────────
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
  const silPts = await sampleSilhouette();
  init(silPts);
  requestAnimationFrame(animate);
})();
