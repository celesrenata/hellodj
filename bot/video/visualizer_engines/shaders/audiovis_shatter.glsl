#version 330 core

// Shatter — Voronoi tessellation simulating glass fractures.
// ~20 seed points drift slowly with iTime. Beat injects new seeds radiating
// from center. Fracture decay proportional to 1/iBPM. Cell borders rendered
// as bright white cracks. Cells offset by fracture intensity.

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

#define NUM_SEEDS 20
#define PI 3.14159265359

// --- Utility: deterministic hash functions (self-contained, no #include) ---

float hash11(float p) {
    p = fract(p * 0.1031);
    p *= p + 33.33;
    p *= p + p;
    return fract(p);
}

vec2 hash21(float p) {
    vec3 p3 = fract(vec3(p) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

vec2 hash22(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * vec3(0.1031, 0.1030, 0.0973));
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.xx + p3.yz) * p3.zy);
}

// --- Voronoi with fixed seed point array ---

// Generate a seed position for index i, drifting slowly with time (Req 8 AC 1)
vec2 seedPosition(int i, float time) {
    vec2 base = hash21(float(i) * 7.31 + 0.5);
    // Slow continuous drift ensures animation even with zero audio
    float driftSpeed = 0.08 + hash11(float(i) * 3.17) * 0.04;
    vec2 drift = vec2(
        sin(time * driftSpeed + float(i) * 1.7),
        cos(time * driftSpeed * 0.8 + float(i) * 2.3)
    );
    return base + drift * 0.06;
}

void main() {
    // Aspect-corrected UV centered at origin
    vec2 uv = vUV;
    vec2 uvCentered = uv - 0.5;
    uvCentered.x *= iResolution.x / iResolution.y;

    // --- Fracture intensity from beat with BPM-proportional decay ---
    // Fracture decays proportional to 1/iBPM: slower BPM = longer visible fractures
    // iBeat is already a decaying pulse (1.0→0.0), but we modulate its impact by BPM
    float bpmFactor = clamp(120.0 / max(iBPM, 60.0), 0.6, 2.0);
    float fractureIntensity = iBeat * bpmFactor;

    // Band energy modulation (Req 7 AC 3 — uses iBandEnergy[0], [2], [5])
    // Sub-bass drives cell displacement magnitude
    float displacement = iBandEnergy[0] * 0.04;
    // Low-mid drives crack brightness
    float crackBrightness = 1.5 + iBandEnergy[2] * 3.0;
    // Presence drives seed jitter (high-frequency detail)
    float seedJitter = iBandEnergy[5] * 0.02;

    // --- Compute fracture offset per-fragment ---
    // Cells offset from center by distance × fracture intensity
    float distFromCenter = length(uvCentered);
    vec2 fractureOffset = normalize(uvCentered + 0.001) * distFromCenter * fractureIntensity * 0.15;

    // Apply fracture offset to UV for voronoi lookup
    vec2 sampleUV = uv + fractureOffset + uvCentered * displacement;

    // --- Voronoi tessellation with NUM_SEEDS fixed points ---
    float minDist = 1e10;
    float secondDist = 1e10;
    int closestSeed = 0;
    vec2 closestPoint = vec2(0.0);

    for (int i = 0; i < NUM_SEEDS; i++) {
        // Base seed position with slow time drift
        vec2 seed = seedPosition(i, iTime);

        // Beat injects perturbation radiating from center (Req 7 AC 1 — structural change)
        // When beat fires, seeds temporarily shift outward from center
        vec2 seedCenter = seed - 0.5;
        float seedCenterDist = length(seedCenter);
        vec2 beatRadiate = normalize(seedCenter + 0.001) * fractureIntensity * 0.12;
        seed += beatRadiate;

        // High-frequency jitter from presence band
        seed += vec2(
            sin(iTime * 3.0 + float(i) * 5.1),
            cos(iTime * 2.7 + float(i) * 4.3)
        ) * seedJitter;

        // Distance from sample point to this seed
        vec2 diff = sampleUV - seed;
        // Correct aspect ratio for distance
        diff.x *= iResolution.x / iResolution.y;
        float d = length(diff);

        if (d < minDist) {
            secondDist = minDist;
            minDist = d;
            closestSeed = i;
            closestPoint = seed;
        } else if (d < secondDist) {
            secondDist = d;
        }
    }

    // --- Edge detection: cell borders as bright cracks ---
    float edgeDist = secondDist - minDist;

    // Crack width narrows slightly on beat for sharper fracture look
    float crackWidth = 0.025 - fractureIntensity * 0.01;
    crackWidth = max(crackWidth, 0.008);

    // Smooth edge factor (1.0 at edge, 0.0 in cell interior)
    float edge = 1.0 - smoothstep(0.0, crackWidth, edgeDist);

    // Bright white crack color (Req 7 AC 2 — crack brightness differs during beat)
    vec3 crackColor = vec3(1.0, 0.95, 0.9) * crackBrightness;

    // --- Cell interior: refracted gradient background ---
    // Per-cell color from seed hash
    float cellHue = hash11(float(closestSeed) * 13.37);

    // Background gradient: deep blue-purple to dark teal
    vec2 gradUV = sampleUV + fractureOffset * 0.5;
    vec3 bgGradient = mix(
        vec3(0.02, 0.01, 0.06),  // deep purple-black
        vec3(0.01, 0.04, 0.08),  // dark teal
        gradUV.y
    );

    // Cell tint based on hash — gives each shard a unique refraction
    vec3 cellTint = vec3(
        0.5 + 0.5 * sin(cellHue * 6.28 + 0.0),
        0.5 + 0.5 * sin(cellHue * 6.28 + 2.094),
        0.5 + 0.5 * sin(cellHue * 6.28 + 4.189)
    );

    // Refracted interior — shifted background + subtle cell color
    vec3 interior = bgGradient + cellTint * 0.08;

    // Distance-from-center darkening for depth
    interior *= 1.0 - distFromCenter * 0.3;

    // --- Compose final color ---
    vec3 col = mix(interior, crackColor, edge);

    // Beat pulse: overall brightness surge (Req 7 AC 2 — 2+ params differ during peak)
    col *= 1.0 + fractureIntensity * 0.6;

    // Glow around cracks — wider soft glow
    float glowEdge = 1.0 - smoothstep(0.0, crackWidth * 4.0, edgeDist);
    col += vec3(0.4, 0.3, 0.6) * glowEdge * 0.3 * (1.0 + fractureIntensity);

    // Glow intensity uniform modulation
    col *= 1.0 + iGlowIntensity * 0.25;

    // Subtle vignette
    float vig = 1.0 - distFromCenter * 0.4;
    col *= vig;

    // Alpha compositing with background opacity
    float alpha = mix(iBgOpacity, 1.0, edge * 0.5 + 0.5);

    fragColor = vec4(col, alpha);
}
