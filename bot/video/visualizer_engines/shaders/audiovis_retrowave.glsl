#version 330 core

// Retrowave — VHS artifacts (chromatic aberration, scanlines, tape warp) over
// neon SDF geometric shapes. Beat multiplies all distortion by 3x.
// iBandEnergy[2..4] controls neon shape pulse intensity.
// Continuous tape-roll effect from iTime. Hot pink/electric blue/neon purple palette.

in vec2 vUV;
out vec4 fragColor;

uniform float     iTime;
uniform vec2      iResolution;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];
uniform sampler1D iFFT;
uniform int       iFFTBins;
uniform float     iGlowIntensity;
uniform float     iBgOpacity;

#define PI  3.14159265359
#define TAU 6.28318530718

// --- Self-contained utilities (no #include) ---

float hash(float n) {
    return fract(sin(n) * 43758.5453123);
}

float hash2(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash2(i);
    float b = hash2(i + vec2(1.0, 0.0));
    float c = hash2(i + vec2(0.0, 1.0));
    float d = hash2(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// --- SDF primitives ---

float sdCircle(vec2 p, float r) {
    return length(p) - r;
}

float sdTriangle(vec2 p, float r) {
    const float k = sqrt(3.0);
    p.x = abs(p.x) - r;
    p.y = p.y + r / k;
    if (p.x + k * p.y > 0.0) {
        p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
    }
    p.x -= clamp(p.x, -2.0 * r, 0.0);
    return -length(p) * sign(p.y);
}

float sdBox(vec2 p, vec2 b) {
    vec2 d = abs(p) - b;
    return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}

float sdHexagon(vec2 p, float r) {
    const vec3 k = vec3(-0.866025404, 0.5, 0.577350269);
    p = abs(p);
    p -= 2.0 * min(dot(k.xy, p), 0.0) * k.xy;
    p -= vec2(clamp(p.x, -k.z * r, k.z * r), r);
    return length(p) * sign(p.y);
}

// Rotation matrix
mat2 rot2(float a) {
    float c = cos(a), s = sin(a);
    return mat2(c, -s, s, c);
}

// --- Neon palette (hot pink / electric blue / neon purple) ---

vec3 neonColor(float t) {
    // Cycle through: hot pink (1.0, 0.1, 0.5), electric blue (0.1, 0.4, 1.0), neon purple (0.7, 0.1, 0.9)
    vec3 pink   = vec3(1.0, 0.08, 0.58);
    vec3 blue   = vec3(0.1, 0.4, 1.0);
    vec3 purple = vec3(0.7, 0.1, 0.9);

    float phase = fract(t);
    if (phase < 0.333) {
        return mix(pink, blue, phase * 3.0);
    } else if (phase < 0.666) {
        return mix(blue, purple, (phase - 0.333) * 3.0);
    } else {
        return mix(purple, pink, (phase - 0.666) * 3.0);
    }
}

// --- Neon SDF shape layer ---
// Draws glowing outlined shapes. iBandEnergy[2..4] drives pulse intensity.

vec3 neonShapes(vec2 uv, float aspect) {
    vec3 col = vec3(0.0);

    // Shape pulse intensity from mid bands (Req 7 AC 3: bands 2, 3, 4)
    float pulse2 = iBandEnergy[2];
    float pulse3 = iBandEnergy[3];
    float pulse4 = iBandEnergy[4];

    // --- Shape 1: rotating triangle (upper-left) ---
    {
        vec2 p = uv - vec2(0.3, 0.7);
        p.x *= aspect;
        p *= rot2(iTime * 0.4 + pulse2 * TAU);
        float size = 0.10 + pulse2 * 0.04;
        float d = sdTriangle(p, size);
        float glow = 0.004 / (abs(d) + 0.004);
        col += neonColor(iTime * 0.2) * glow * (0.5 + pulse2 * 1.5);
    }

    // --- Shape 2: pulsing circle (center) ---
    {
        vec2 p = uv - vec2(0.5, 0.5);
        p.x *= aspect;
        float size = 0.12 + pulse3 * 0.06;
        float d = sdCircle(p, size);
        float glow = 0.005 / (abs(d) + 0.005);
        col += neonColor(iTime * 0.15 + 0.33) * glow * (0.6 + pulse3 * 1.4);
    }

    // --- Shape 3: rotating hexagon (lower-right) ---
    {
        vec2 p = uv - vec2(0.7, 0.3);
        p.x *= aspect;
        p *= rot2(-iTime * 0.3 + pulse4 * PI);
        float size = 0.09 + pulse4 * 0.05;
        float d = sdHexagon(p, size);
        float glow = 0.004 / (abs(d) + 0.004);
        col += neonColor(iTime * 0.25 + 0.66) * glow * (0.5 + pulse4 * 1.5);
    }

    // --- Shape 4: diamond/rotated box (upper-right) ---
    {
        vec2 p = uv - vec2(0.72, 0.72);
        p.x *= aspect;
        p *= rot2(PI * 0.25 + iTime * 0.5);
        float size = 0.06 + pulse2 * 0.03;
        float d = sdBox(p, vec2(size));
        float glow = 0.003 / (abs(d) + 0.003);
        col += neonColor(iTime * 0.3 + 0.5) * glow * (0.4 + pulse3 * 1.2);
    }

    // --- Shape 5: small circle orbit (lower-left) ---
    {
        float orbitAngle = iTime * 0.7;
        vec2 center = vec2(0.28, 0.28);
        vec2 offset = vec2(cos(orbitAngle), sin(orbitAngle)) * 0.08;
        vec2 p = uv - (center + offset);
        p.x *= aspect;
        float size = 0.04 + pulse4 * 0.03;
        float d = sdCircle(p, size);
        float glow = 0.003 / (abs(d) + 0.003);
        col += neonColor(iTime * 0.35 + 0.8) * glow * (0.5 + pulse2 * 1.0);
    }

    return col;
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // === Beat distortion multiplier ===
    // Req 7 AC 1: Beat multiplies all distortion by 3x (structural change)
    // distortion *= 1.0 + iBeat * 2.0  →  max 3x at iBeat=1.0
    float beatMult = 1.0 + iBeat * 2.0;

    // === VHS tape warp (sinusoidal UV distortion) ===
    // Continuous tape-roll driven by iTime (Req 8 AC 1)
    float tapeRoll = iTime * 0.8;
    float warpStrength = (0.005 + iBandEnergy[3] * 0.008) * beatMult;

    // Tape wobble: horizontal displacement varies along vertical
    float warp = sin(uv.y * 12.0 + tapeRoll * 3.0) * warpStrength;
    warp += sin(uv.y * 25.0 - tapeRoll * 5.0) * warpStrength * 0.5;
    // Additional warp band that drifts (simulates tape head misalignment)
    float tapeHead = fract(tapeRoll * 0.15);
    float headDist = abs(uv.y - tapeHead);
    float headWarp = smoothstep(0.08, 0.0, headDist) * 0.03 * beatMult;
    warp += headWarp;

    vec2 warpedUV = vec2(uv.x + warp, uv.y);
    warpedUV = clamp(warpedUV, 0.0, 1.0);

    // === Chromatic aberration (RGB offset) ===
    float chromaAmount = (0.004 + iBandEnergy[2] * 0.008) * beatMult;
    // Vertical chromatic offset (VHS style — RGB offset is horizontal)
    vec2 offsetR = vec2( chromaAmount, 0.0);
    vec2 offsetB = vec2(-chromaAmount, 0.0);

    vec2 uvR = clamp(warpedUV + offsetR, 0.0, 1.0);
    vec2 uvG = warpedUV;
    vec2 uvB = clamp(warpedUV + offsetB, 0.0, 1.0);

    // === Render neon shapes at each channel's UV ===
    vec3 sceneR = neonShapes(uvR, aspect);
    vec3 sceneG = neonShapes(uvG, aspect);
    vec3 sceneB = neonShapes(uvB, aspect);

    // Combine with chromatic split
    vec3 col = vec3(sceneR.r, sceneG.g, sceneB.b);

    // Add full-color base from the green channel UV (keeps shape brightness)
    col += neonShapes(uvG, aspect) * 0.3;

    // === Dark gradient background ===
    vec3 bg = mix(vec3(0.02, 0.01, 0.04), vec3(0.05, 0.01, 0.08), uv.y);
    // Subtle purple vignette
    float vignette = 1.0 - length((uv - 0.5) * 1.4);
    bg *= 0.8 + vignette * 0.2;
    col += bg;

    // === Horizontal scanlines ===
    // Classic VHS scanlines — intensity modulated by tape roll (Req 8 AC 1)
    float scanlineFreq = iResolution.y * 0.5;
    float scanline = sin(uv.y * scanlineFreq * PI + tapeRoll * 2.0) * 0.5 + 0.5;
    float scanlineStrength = (0.15 + iBandEnergy[4] * 0.15) * beatMult;
    scanlineStrength = min(scanlineStrength, 0.7); // cap to avoid total blackout
    col *= 1.0 - scanline * scanlineStrength;

    // Thicker scanline bands (VHS tracking lines)
    float trackingLine = fract(tapeRoll * 0.2);
    float trackDist = abs(uv.y - trackingLine);
    float trackBand = smoothstep(0.015, 0.0, trackDist) * 0.4 * beatMult;
    col = mix(col, vec3(0.8, 0.2, 0.9), trackBand);

    // === VHS noise (tape hiss / static) ===
    float noiseStrength = (0.03 + iBeat * 0.08) * beatMult * 0.5;
    float staticNoise = hash2(uv * iResolution + vec2(iTime * 137.0, iTime * 241.0));
    col += vec3(staticNoise) * noiseStrength;

    // === Color bleeding (VHS color smear) ===
    // Slight horizontal color bleed on bright areas
    float bleed = neonShapes(vec2(clamp(warpedUV.x - 0.01, 0.0, 1.0), warpedUV.y), aspect).r;
    col.r += bleed * 0.08 * beatMult;

    // === Rolling black bar (tape roll artifact — Req 8 AC 1) ===
    float rollBar = fract(tapeRoll * 0.1);
    float barDist = abs(uv.y - rollBar);
    float bar = smoothstep(0.04, 0.0, barDist);
    col *= 1.0 - bar * 0.5;

    // === Final adjustments ===
    // Glow intensity
    col *= 1.0 + iGlowIntensity * 0.4;

    // Background opacity
    col *= iBgOpacity;

    // Clamp output
    col = clamp(col, 0.0, 1.0);

    // --- Requirement compliance notes ---
    // Req 7 AC 1: Beat multiplies all distortion by 3x (beatMult = 1.0 + iBeat * 2.0)
    //   Structural changes: tape warp intensifies, chroma splits wider, scanlines deepen,
    //   tracking lines become more visible, static noise increases.
    //
    // Req 7 AC 2: While iBeat > 0.5, 2+ parameters differ from resting:
    //   1. warpStrength amplified
    //   2. chromaAmount amplified
    //   3. scanlineStrength amplified
    //   4. noiseStrength amplified
    //   5. trackBand intensity amplified
    //
    // Req 7 AC 3: Uses minimum 3 distinct iBandEnergy bands:
    //   - iBandEnergy[2] (low-mid): chromatic aberration amount + shape pulse
    //   - iBandEnergy[3] (mid): tape warp strength + shape pulse
    //   - iBandEnergy[4] (upper-mid): scanline intensity + shape pulse
    //
    // Req 8 AC 1: Continuous tape-roll from iTime:
    //   - tapeRoll drives warp phase, scanline position, tracking line, rolling bar
    //   - Shape rotation + orbits driven by iTime
    //   - All produce frame-to-frame change with zero audio input

    fragColor = vec4(col, 1.0);
}
