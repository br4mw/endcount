/**
 * Endcount — Species Self-Portrait
 *
 * Each stamp = one individual still alive in the wild.
 * Extinct species: blank canvas. 0 remain.
 * Stamp positions are pre-computed server-side for reliability.
 */

let spriteImg;
let allCandidates = [];   // from /api/stamps/
let stamps = [];          // final selected stamps to draw
let stampIdx = 0;
let ready = false;

const CAT_COLORS = {
  EX: [61,  43,  31],
  EW: [92,  61,  46],
  CR: [139, 58,  42],
  EN: [184, 92,  56],
  VU: [196, 135, 74],
  NT: [122, 143, 94],
  LC: [74,  122, 110],
};

function preload() {
  spriteImg = loadImage(
    "/api/sprite-png/" + window.ASSESSMENT_ID,
    () => {},
    () => { spriteImg = null; }
  );
}

function setup() {
  const population     = window.POPULATION;      // integer, 0, or null
  const categoryCode   = window.CATEGORY || "EX";
  const scientificName = window.SCIENTIFIC_NAME || "";

  const container = document.getElementById("art-canvas-container");
  const w = container.offsetWidth || 800;
  const h = Math.round(w * 0.56);

  const cnv = createCanvas(w, h);
  cnv.parent("art-canvas-container");
  colorMode(RGB);
  noStroke();
  background(246, 245, 241);

  // Extinct: blank canvas
  if (population === 0) {
    drawExtinctState(scientificName, categoryCode);
    noLoop();
    return;
  }

  if (!spriteImg) {
    noLoop();
    return;
  }

  // Fetch pre-computed stamp positions from server, then build stamps
  fetch("/api/stamps/" + window.ASSESSMENT_ID)
    .then(r => r.json())
    .then(data => {
      buildStamps(data, population, categoryCode);
      ready = true;
      frameRate(60);
      loop();
    })
    .catch(() => noLoop());

  noLoop(); // wait for fetch
}

function buildStamps(data, population, categoryCode) {
  const catColor = CAT_COLORS[categoryCode] || [80, 60, 40];
  const canvasW = width;
  const canvasH = height;

  let candidates = data.stamps; // [{x:0..1, y:0..1, r,g,b,bright}, ...]

  // Determine how many stamps to draw
  // null = unknown population → fill as many as fit (cap 6000)
  const maxCount = population === null
    ? Math.min(candidates.length, 6000)
    : Math.min(population, candidates.length);

  const selected = candidates.slice(0, maxCount);

  // Sprite size: rarer species → larger, more prominent stamps
  const baseSize = map(maxCount, 0, 5000, 28, 5, true);

  stamps = selected.map(c => {
    const rotation = (Math.random() - 0.5) * (Math.PI / 10);
    const size = baseSize * (0.8 + Math.random() * 0.4);

    // A rotated square's bounding half-extent = (size/2) * (|cosθ| + |sinθ|).
    // Use this as the inset margin so no part of the sprite leaves the canvas.
    const halfExtent = (size / 2) * (Math.abs(Math.cos(rotation)) + Math.abs(Math.sin(rotation)));

    const rawX = c.x * canvasW;
    const rawY = c.y * canvasH;

    return {
      x: Math.max(halfExtent, Math.min(canvasW - halfExtent, rawX)),
      y: Math.max(halfExtent, Math.min(canvasH - halfExtent, rawY)),
      r: Math.round(lerp(c.r, catColor[0], 0.25)),
      g: Math.round(lerp(c.g, catColor[1], 0.25)),
      b: Math.round(lerp(c.b, catColor[2], 0.25)),
      size,
      rotation,
      alpha: map(c.bright, 0, 220, 215, 90),
    };
  });
}

function draw() {
  if (!ready || stampIdx >= stamps.length) {
    if (ready && stampIdx >= stamps.length) {
      drawCountLabel();
      noLoop();
    }
    return;
  }

  // Batch size: finish in ~4 seconds at 60fps
  const batchSize = Math.max(1, Math.ceil(stamps.length / (4 * 60)));

  imageMode(CENTER);
  for (let i = 0; i < batchSize && stampIdx < stamps.length; i++, stampIdx++) {
    const s = stamps[stampIdx];
    push();
    translate(s.x, s.y);
    rotate(s.rotation);
    tint(s.r, s.g, s.b, s.alpha);
    image(spriteImg, 0, 0, s.size, s.size);
    pop();
  }
}

function drawExtinctState(scientificName, categoryCode) {
  const catColor = CAT_COLORS[categoryCode] || [61, 43, 31];
  fill(catColor[0], catColor[1], catColor[2], 18);
  rect(0, 0, width, height);

  textAlign(CENTER, CENTER);
  fill(180, 170, 160);
  textSize(10);
  text("0  R E M A I N", width / 2, height / 2 - 16);

  fill(150, 140, 130);
  textSize(13);
  drawingContext.font = "italic 13px Georgia, serif";
  text(scientificName, width / 2, height / 2 + 14);
}

function drawCountLabel() {
  const population = window.POPULATION;
  const label = population === null
    ? "population unknown"
    : population.toLocaleString() + " remain";

  const pad = 14;
  push();
  textSize(10);
  textAlign(RIGHT, BOTTOM);
  const tw = textWidth(label);
  fill(246, 245, 241, 210);
  noStroke();
  rect(width - tw - pad * 2, height - 30, tw + pad * 2, 26, 2);
  fill(130, 120, 110);
  text(label, width - pad, height - 9);
  pop();
}

function windowResized() {
  const container = document.getElementById("art-canvas-container");
  const w = container.offsetWidth || 800;
  resizeCanvas(w, Math.round(w * 0.56));
  background(246, 245, 241);
  stamps = [];
  stampIdx = 0;
  ready = false;

  const population   = window.POPULATION;
  const categoryCode = window.CATEGORY || "EX";

  if (population === 0) {
    drawExtinctState(window.SCIENTIFIC_NAME || "", categoryCode);
    return;
  }

  fetch("/api/stamps/" + window.ASSESSMENT_ID)
    .then(r => r.json())
    .then(data => {
      buildStamps(data, population, categoryCode);
      ready = true;
      loop();
    });
}
