// KARAOKE APP — procedural deep-space background.
//
// A fullscreen WebGL shader draws the nebulae, dust lanes, and galaxy. The
// star field is a transparent texture generated at runtime with Canvas2D, so
// the page needs no external media assets and makes no extra network requests.
//
// Only the host page loads this module. It renders at a capped rate, pauses
// while the tab is hidden, and respects prefers-reduced-motion.

(() => {
  "use strict";

  const canvas = document.getElementById("background-canvas");
  if (!canvas) return;

  const SCENE_NEBULA = 0;
  const SCENE_MILKY_WAY = 1;
  const SCENE_SPIRAL = 2;
  const SCENE_ORDER = [SCENE_NEBULA, SCENE_MILKY_WAY, SCENE_SPIRAL];
  const SCENE_SECONDS = 24;
  const FADE_SECONDS = 4;
  const MAX_RENDER_WIDTH = 1600;
  const MAX_RENDER_HEIGHT = 900;
  const MAX_DPR = 1.25;
  const FRAME_MS = 1000 / 30;
  const TEXTURE_SIZE = 1024;

  const motionQuery = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener: null };

  let gl = null;
  let program = null;
  let uniforms = null;
  let starTexture = null;
  let starSource = null;
  let raf = 0;
  let running = false;
  let lastTime = 0;
  let lastDraw = 0;
  let elapsed = 0;
  let seed = Math.random() * Math.PI * 2;
  let quality = 1;
  let statStart = 0;
  let statFrames = 0;
  let statTotal = 0;
  let fallbackWarned = false;

  function mulberry32(seedValue) {
    let a = seedValue >>> 0;
    return function random() {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), 1 | t);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function makeStarSprite(size, rgb) {
    const sprite = document.createElement("canvas");
    sprite.width = size;
    sprite.height = size;
    const ctx = sprite.getContext("2d");
    const half = size / 2;
    const gradient = ctx.createRadialGradient(half, half, 0, half, half, half);
    const [r, g, b] = rgb;
    gradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, 1)`);
    gradient.addColorStop(0.28, `rgba(${r}, ${g}, ${b}, 0.55)`);
    gradient.addColorStop(0.62, `rgba(${r}, ${g}, ${b}, 0.14)`);
    gradient.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(half, half, half, 0, Math.PI * 2);
    ctx.fill();
    return sprite;
  }

  function createStarSource() {
    const source = document.createElement("canvas");
    source.width = TEXTURE_SIZE;
    source.height = TEXTURE_SIZE;
    const ctx = source.getContext("2d");
    const random = mulberry32(0x5f3759df);
    const colors = [
      [255, 255, 255],
      [198, 218, 255],
      [255, 227, 198],
      [255, 186, 148]
    ];
    const weights = [0.68, 0.17, 0.11, 0.04];
    const sizes = [4, 6, 10];
    const sprites = colors.map(color => sizes.map(size => makeStarSprite(size, color)));

    function pickColor() {
      let roll = random();
      for (let i = 0; i < weights.length; i++) {
        roll -= weights[i];
        if (roll <= 0) return i;
      }
      return 0;
    }

    function drawStars(count, sizeIndex, alphaMin, alphaMax) {
      for (let i = 0; i < count; i++) {
        const color = pickColor();
        const sprite = sprites[color][sizeIndex];
        const x = random() * TEXTURE_SIZE;
        const y = random() * TEXTURE_SIZE;
        const alpha = alphaMin + random() * (alphaMax - alphaMin);
        const half = sprite.width / 2;
        ctx.globalAlpha = alpha;
        ctx.drawImage(sprite, x - half, y - half);
        // Wrap stars that touch an edge so the repeating texture has no seam.
        if (x < half) ctx.drawImage(sprite, x + TEXTURE_SIZE - half, y - half);
        if (x > TEXTURE_SIZE - half) ctx.drawImage(sprite, x - TEXTURE_SIZE - half, y - half);
        if (y < half) ctx.drawImage(sprite, x - half, y + TEXTURE_SIZE - half);
        if (y > TEXTURE_SIZE - half) ctx.drawImage(sprite, x - half, y - TEXTURE_SIZE - half);
      }
    }

    ctx.globalCompositeOperation = "lighter";
    drawStars(900, 0, 0.45, 1);
    drawStars(130, 1, 0.6, 1);
    drawStars(24, 2, 0.8, 1);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    return source;
  }

  const VERTEX_SHADER = `
attribute vec2 aPosition;
varying vec2 vUv;

void main() {
  vUv = aPosition * 0.5 + 0.5;
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

  const FRAGMENT_SHADER = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform vec2 uResolution;
uniform float uTime;
uniform float uSceneA;
uniform float uSceneB;
uniform float uMix;
uniform float uSeed;
uniform sampler2D uStars;

varying vec2 vUv;

float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  float a = hash12(i);
  float b = hash12(i + vec2(1.0, 0.0));
  float c = hash12(i + vec2(0.0, 1.0));
  float d = hash12(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float value = 0.0;
  float amplitude = 0.5;
  mat2 rotation = mat2(0.80, 0.60, -0.60, 0.80);
  for (int i = 0; i < 4; i++) {
    value += amplitude * noise(p);
    p = rotation * p * 2.03 + 17.13;
    amplitude *= 0.5;
  }
  return value;
}

vec2 cameraDrift(vec2 uv) {
  vec2 p = uv - 0.5;
  float zoom = 1.0 + 0.032 * sin(uTime * 0.075);
  p *= zoom;
  p += vec2(0.010 * sin(uTime * 0.047), 0.006 * cos(uTime * 0.033));
  return p + 0.5;
}

vec3 applyStars(vec2 uv, float density) {
  vec3 nearStars = texture2D(uStars, uv * 1.65 + vec2(uTime * 0.0040, uTime * 0.0013)).rgb;
  vec3 farStars = texture2D(uStars, uv * 2.85 + vec2(0.37 - uTime * 0.0024, 0.61 + uTime * 0.0008)).rgb;
  // Use the sampled brightness as a per-star phase so the twinkle is not a
  // uniform pulse across the whole field.
  float twinkle = 0.75 + 0.25 * sin(uTime * 1.7 + dot(nearStars, vec3(11.0, 7.0, 5.0)));
  return (nearStars * twinkle + farStars * 0.60) * density * 1.35;
}

vec3 sceneNebula(vec2 uv) {
  float aspect = uResolution.x / uResolution.y;
  float breathe = 1.0 + 0.035 * sin(uTime * 0.090);
  vec2 p = (uv - 0.5) * vec2(aspect, 1.0) * breathe;
  float t = uTime * 0.040;

  vec2 q = vec2(
    fbm(p * 2.0 + vec2(3.1, 1.7) + t),
    fbm(p * 2.0 + vec2(7.4, 9.2) - t)
  );
  vec2 r = vec2(
    fbm(p * 2.0 + 3.2 * q + vec2(1.7, 9.2) + t * 1.4),
    fbm(p * 2.0 + 3.2 * q + vec2(8.3, 2.8) - t * 1.2)
  );
  float f = fbm(p * 2.0 + 2.8 * r + uSeed);

  float density = smoothstep(0.36, 0.68, f);
  float fine = smoothstep(0.44, 0.74, fbm(p * 6.0 + r * 2.2 + 11.0));

  vec3 deep = vec3(0.008, 0.014, 0.042);
  vec3 indigo = vec3(0.055, 0.065, 0.22);
  vec3 magenta = vec3(0.62, 0.06, 0.38);
  vec3 cyan = vec3(0.10, 0.52, 0.65);
  vec3 warm = vec3(1.0, 0.80, 0.62);

  vec3 color = deep;
  color = mix(color, indigo, smoothstep(0.08, 0.55, f));
  color = mix(color, magenta, smoothstep(0.32, 0.72, f) * (0.55 + 0.45 * q.x));
  color = mix(color, cyan, smoothstep(0.40, 0.82, r.x) * 0.90);
  color += warm * pow(smoothstep(0.56, 0.96, f), 2.0) * 0.55;

  // Small bright star and a soft halo near the top, echoing the nebula clip.
  vec2 core = (uv - vec2(0.62, 0.84)) * vec2(aspect, 1.0);
  float coreDistance = dot(core, core);
  color += vec3(0.95, 0.98, 1.0) * exp(-coreDistance * 6000.0) * 2.6;
  color += vec3(0.45, 0.62, 0.92) * exp(-coreDistance * 80.0) * 0.18;

  color *= 1.0 - 0.62 * fine;
  color += applyStars(uv, 0.45 + 0.95 * density);

  float radius = length((uv - 0.5) * vec2(aspect, 1.0));
  color *= 0.60 + 0.40 * (1.0 - smoothstep(0.30, 1.10, radius));

  return 1.0 - exp(-color * 1.85);
}

vec3 sceneMilkyWay(vec2 uv) {
  float aspect = uResolution.x / uResolution.y;
  vec2 p = (uv - 0.5) * vec2(aspect, 1.0);
  float angle = 0.42 + uTime * 0.0035;
  float c = cos(angle);
  float s = sin(angle);
  p = mat2(c, -s, s, c) * p;

  float t = uTime * 0.012;
  float center = 0.30 + 0.055 * sin(p.x * 1.7 + 0.6) + 0.018 * sin(p.x * 4.1 - 1.3);
  float d = p.y - center;
  float width = 0.115 + 0.025 * fbm(vec2(p.x * 1.6, 3.2) + t);
  float band = exp(-(d * d) / (width * width));
  float halo = exp(-(d * d) / (width * width * 7.0));

  // Fine filamentary dust lanes running along the band.
  float filament = fbm(vec2(p.x * 2.4 + t * 2.0, p.y * 10.0));
  float dust = smoothstep(0.30, 0.78, filament) * band;

  // Break the smooth Gaussian into uneven star clouds.
  float structure = fbm(vec2(p.x * 2.6 + t * 1.4, p.y * 7.5));
  float bandLight = band * (0.75 + 0.50 * structure);
  float coreLine = exp(-(d * d) / (width * width * 0.10));

  vec3 deep = vec3(0.004, 0.007, 0.017);
  vec3 blue = vec3(0.10, 0.16, 0.30);
  vec3 cream = vec3(1.0, 0.91, 0.74);
  vec3 warm = vec3(1.0, 0.82, 0.56);

  vec3 color = deep;
  color += blue * halo * 0.26;
  color += cream * pow(bandLight, 3.2) * 0.82;
  color += warm * coreLine * 0.38;
  // Cool the outer band and warm the inner clouds.
  vec3 tint = mix(vec3(0.55, 0.38, 0.62), vec3(0.32, 0.48, 0.80), smoothstep(-0.6, 0.7, p.x));
  color += tint * pow(band, 1.6) * 0.16;

  // Soft galactic bulge toward one side.
  vec2 bulge = p - vec2(-0.22, center);
  float bulgeGlow = exp(-dot(bulge, bulge) * 9.0);
  color += vec3(0.98, 0.82, 0.58) * bulgeGlow * 0.40;

  color *= 1.0 - 0.58 * dust;
  color += applyStars(uv, 0.30 + 1.60 * band + 0.55 * bulgeGlow);
  // Extra dense star grain so the band reads as stars, not a smooth cloud.
  vec3 grain = texture2D(uStars, uv * 5.4 + vec2(uTime * 0.0025, uTime * 0.0011)).rgb;
  color += grain * (0.22 + 2.10 * pow(band, 1.2));
  vec3 grain2 = texture2D(uStars, uv * 9.3 + vec2(0.51 - uTime * 0.0016, 0.13)).rgb;
  color += grain2 * (0.10 + 1.40 * band);

  float radius = length((uv - 0.5) * vec2(aspect, 1.0));
  color *= 0.62 + 0.38 * (1.0 - smoothstep(0.30, 1.15, radius));

  return 1.0 - exp(-color * 1.70);
}

vec3 sceneSpiral(vec2 uv) {
  float aspect = uResolution.x / uResolution.y;
  vec2 p = (uv - vec2(0.50, 0.66)) * vec2(aspect, 1.0);
  p.y /= 0.62;  // Tilt the disk so it reads as a slanted ellipse.

  float rot = uTime * 0.018;
  float c = cos(rot);
  float s = sin(rot);
  p = mat2(c, -s, s, c) * p;

  float r = length(p);
  float a = atan(p.y, p.x);
  float phase = 2.0 * a - 6.5 * log(r + 0.05);
  float spiral = sin(phase);
  float arm = pow(max(spiral, 0.0), 4.0);

  float cloud = fbm(p * 5.0 + vec2(3.0, 1.5) + uSeed + uTime * 0.010);
  float armGlow = arm * exp(-r * 2.4) * (0.35 + 0.95 * cloud);

  float halo = exp(-r * 2.8);
  float core = exp(-r * r * 400.0);
  float coreGlow = exp(-r * r * 28.0);

  vec3 deep = vec3(0.005, 0.006, 0.016);
  vec3 blue = vec3(0.22, 0.38, 0.72);
  vec3 pale = vec3(0.78, 0.82, 1.0);
  vec3 warm = vec3(1.0, 0.87, 0.64);
  vec3 pink = vec3(0.95, 0.32, 0.52);

  vec3 color = deep;
  color += blue * halo * 0.18;
  color += pale * armGlow * 1.05;
  color += warm * coreGlow * 0.65;
  color += vec3(1.0, 0.97, 0.92) * core * 1.70;

  // Pink star-forming knots scattered along the arms.
  float knots = smoothstep(0.62, 0.95, fbm(p * 8.0 + 5.0));
  color += pink * knots * armGlow * 0.55;

  // Dark dust lanes hug the inside edge of each arm.
  float lane = pow(max(-spiral, 0.0), 6.0) * exp(-r * 2.4) * 0.7;
  color *= 1.0 - 0.55 * lane;

  color += applyStars(uv, 0.25 + 1.60 * armGlow);
  vec3 grain = texture2D(uStars, uv * 6.2 + vec2(uTime * 0.0020, 0.30)).rgb;
  color += grain * (0.15 + 1.35 * armGlow);

  float radius = length((uv - 0.5) * vec2(aspect, 1.0));
  color *= 0.62 + 0.38 * (1.0 - smoothstep(0.30, 1.15, radius));

  return 1.0 - exp(-color * 1.35);
}

vec3 renderScene(float scene, vec2 uv) {
  if (scene < 0.5) return sceneNebula(uv);
  if (scene < 1.5) return sceneMilkyWay(uv);
  return sceneSpiral(uv);
}

float meteor(vec2 uv, float t) {
  float cycle = 34.0;
  float slot = floor(t / cycle);
  float local = t - slot * cycle;
  float chance = hash12(vec2(slot, 4.7));
  if (chance > 0.55 || local > 2.0) return 0.0;

  float r1 = hash12(vec2(slot, 1.3));
  float r2 = hash12(vec2(slot, 8.1));
  vec2 start = vec2(0.08 + 0.84 * r1, 0.78 + 0.16 * r2);
  vec2 dir = normalize(vec2(0.90, -0.40 - 0.20 * r2));
  float progress = clamp(local / 2.0, 0.0, 1.0);
  vec2 head = start + dir * progress * 0.55;
  vec2 tail = head - dir * (0.04 + 0.04 * r2);
  vec2 segment = head - tail;
  float along = clamp(dot(uv - tail, segment) / dot(segment, segment), 0.0, 1.0);
  float distanceToStreak = length(uv - (tail + segment * along));
  float tailFade = pow(1.0 - along, 1.6);
  float streak = exp(-distanceToStreak * 650.0) * tailFade;
  float headGlow = exp(-length(uv - head) * 1000.0) * 0.8;
  return (streak + headGlow) * 0.8;
}

void main() {
  vec2 uv = cameraDrift(vUv);
  vec3 color = renderScene(uSceneA, uv);
  if (uMix > 0.001) {
    vec3 next = renderScene(uSceneB, uv);
    color = mix(color, next, smoothstep(0.0, 1.0, uMix));
  }
  color += vec3(0.72, 0.86, 1.0) * meteor(vUv, uTime);

  // A little grain plus ordered-ish dithering keeps dark gradients band-free.
  float grain = (hash12(gl_FragCoord.xy + fract(uTime) * 91.7) - 0.5) * 0.008;
  float dither = (hash12(gl_FragCoord.xy * 1.7 + 13.0) - 0.5) / 255.0;
  gl_FragColor = vec4(clamp(color + grain + dither, 0.0, 1.0), 1.0);
}
`;

  function compileShader(gpu, type, source) {
    const shader = gpu.createShader(type);
    gpu.shaderSource(shader, source);
    gpu.compileShader(shader);
    if (!gpu.getShaderParameter(shader, gpu.COMPILE_STATUS)) {
      const log = gpu.getShaderInfoLog(shader);
      gpu.deleteShader(shader);
      throw new Error("Shader compile failed: " + log);
    }
    return shader;
  }

  function createProgram(gpu) {
    const vertex = compileShader(gpu, gpu.VERTEX_SHADER, VERTEX_SHADER);
    const fragment = compileShader(gpu, gpu.FRAGMENT_SHADER, FRAGMENT_SHADER);
    const linked = gpu.createProgram();
    gpu.attachShader(linked, vertex);
    gpu.attachShader(linked, fragment);
    gpu.linkProgram(linked);
    gpu.deleteShader(vertex);
    gpu.deleteShader(fragment);
    if (!gpu.getProgramParameter(linked, gpu.LINK_STATUS)) {
      const log = gpu.getProgramInfoLog(linked);
      gpu.deleteProgram(linked);
      throw new Error("Program link failed: " + log);
    }
    return linked;
  }

  function uploadStarTexture(gpu, source) {
    const texture = gpu.createTexture();
    gpu.bindTexture(gpu.TEXTURE_2D, texture);
    gpu.pixelStorei(gpu.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gpu.texImage2D(gpu.TEXTURE_2D, 0, gpu.RGBA, gpu.RGBA, gpu.UNSIGNED_BYTE, source);
    gpu.generateMipmap(gpu.TEXTURE_2D);
    gpu.texParameteri(gpu.TEXTURE_2D, gpu.TEXTURE_MIN_FILTER, gpu.LINEAR_MIPMAP_LINEAR);
    gpu.texParameteri(gpu.TEXTURE_2D, gpu.TEXTURE_MAG_FILTER, gpu.LINEAR);
    gpu.texParameteri(gpu.TEXTURE_2D, gpu.TEXTURE_WRAP_S, gpu.REPEAT);
    gpu.texParameteri(gpu.TEXTURE_2D, gpu.TEXTURE_WRAP_T, gpu.REPEAT);
    gpu.pixelStorei(gpu.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    return texture;
  }

  function setupGL() {
    const options = {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      preserveDrawingBuffer: false,
      powerPreference: "low-power"
    };
    gl = canvas.getContext("webgl2", options)
      || canvas.getContext("webgl", options)
      || canvas.getContext("experimental-webgl", options);
    if (!gl) {
      if (!fallbackWarned) {
        fallbackWarned = true;
        console.warn("[background] WebGL is unavailable; using the static fallback.");
      }
      return false;
    }

    program = createProgram(gl);

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    );

    const position = gl.getAttribLocation(program, "aPosition");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

    gl.useProgram(program);

    uniforms = {};
    for (const name of ["uResolution", "uTime", "uSceneA", "uSceneB", "uMix", "uSeed", "uStars"]) {
      uniforms[name] = gl.getUniformLocation(program, name);
    }

    starTexture = uploadStarTexture(gl, starSource);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, starTexture);
    if (uniforms.uStars !== null) gl.uniform1i(uniforms.uStars, 0);

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.clearColor(0, 0, 0, 1);
    return true;
  }

  function resize() {
    if (!gl) return;
    const width = canvas.clientWidth || window.innerWidth;
    const height = canvas.clientHeight || window.innerHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    const scale = Math.min(
      1,
      MAX_RENDER_WIDTH / width,
      MAX_RENDER_HEIGHT / height,
      dpr
    ) * quality;
    const renderWidth = Math.max(320, Math.round(width * scale));
    const renderHeight = Math.max(180, Math.round(height * scale));
    if (canvas.width !== renderWidth || canvas.height !== renderHeight) {
      canvas.width = renderWidth;
      canvas.height = renderHeight;
    }
    gl.viewport(0, 0, canvas.width, canvas.height);
    statStart = 0;
    statFrames = 0;
    statTotal = 0;
  }

  function sceneState(time) {
    if (motionQuery.matches) {
      return { a: SCENE_NEBULA, b: SCENE_NEBULA, mix: 0 };
    }
    const cycle = SCENE_SECONDS + FADE_SECONDS;
    const slot = Math.floor(time / cycle);
    const local = time - slot * cycle;
    const a = SCENE_ORDER[slot % SCENE_ORDER.length];
    if (local < SCENE_SECONDS) {
      return { a: a, b: a, mix: 0 };
    }
    const b = SCENE_ORDER[(slot + 1) % SCENE_ORDER.length];
    const raw = (local - SCENE_SECONDS) / FADE_SECONDS;
    const eased = raw * raw * (3 - 2 * raw);
    return { a: a, b: b, mix: eased };
  }

  function draw() {
    if (!gl || !program) return;
    const scene = sceneState(elapsed);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.useProgram(program);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, starTexture);
    if (uniforms.uResolution !== null) gl.uniform2f(uniforms.uResolution, canvas.width, canvas.height);
    if (uniforms.uTime !== null) gl.uniform1f(uniforms.uTime, elapsed);
    if (uniforms.uSceneA !== null) gl.uniform1f(uniforms.uSceneA, scene.a);
    if (uniforms.uSceneB !== null) gl.uniform1f(uniforms.uSceneB, scene.b);
    if (uniforms.uMix !== null) gl.uniform1f(uniforms.uMix, scene.mix);
    if (uniforms.uSeed !== null) gl.uniform1f(uniforms.uSeed, seed);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }

  function stopLoop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  function startLoop() {
    if (running || motionQuery.matches) return;
    running = true;
    lastTime = performance.now();
    lastDraw = 0;
    raf = requestAnimationFrame(frame);
  }

  function frame(now) {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    if (document.hidden) return;

    const dt = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    elapsed += dt;

    if (now - lastDraw < FRAME_MS) return;
    if (lastDraw) {
      statFrames += 1;
      statTotal += now - lastDraw;
      if (!statStart) statStart = now;
      if (now - statStart > 2500) {
        const average = statTotal / statFrames;
        if (average > 48 && quality > 0.55) {
          quality = Math.max(0.55, quality * 0.85);
          resize();
        }
        statStart = 0;
        statFrames = 0;
        statTotal = 0;
      }
    }
    lastDraw = now;
    draw();
  }

  function handleVisibility() {
    if (document.hidden) {
      stopLoop();
    } else {
      startLoop();
    }
  }

  function handleMotionChange() {
    if (motionQuery.matches) {
      stopLoop();
      elapsed = 0;
      draw();
    } else {
      startLoop();
    }
  }

  function handleContextLost(event) {
    event.preventDefault();
    stopLoop();
    canvas.classList.remove("is-ready");
  }

  function handleContextRestored() {
    try {
      if (!setupGL()) return;
    } catch (error) {
      console.warn("[background] Could not restore the procedural background:", error);
      return;
    }
    resize();
    draw();
    canvas.classList.add("is-ready");
    startLoop();
  }

  function init() {
    starSource = createStarSource();

    canvas.addEventListener("webglcontextlost", handleContextLost);
    canvas.addEventListener("webglcontextrestored", handleContextRestored);
    window.addEventListener("resize", () => {
      resize();
      draw();
    });
    document.addEventListener("visibilitychange", handleVisibility);
    if (motionQuery.addEventListener) {
      motionQuery.addEventListener("change", handleMotionChange);
    }

    try {
      if (!setupGL()) return;
    } catch (error) {
      console.warn("[background] Could not start the procedural background:", error);
      canvas.classList.remove("is-ready");
      return;
    }

    resize();
    draw();
    canvas.classList.add("is-ready");
    if (!motionQuery.matches) startLoop();
  }

  init();
})();
