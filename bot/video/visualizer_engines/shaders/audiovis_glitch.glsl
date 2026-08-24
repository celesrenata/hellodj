#version 330 core

// Glitch — Digital corruption: block displacement (8x8 grid), RGB channel
// splitting (chromatic aberration from iBandEnergy[0]), scanline noise flicker.
// Beat multiplies all distortion by 4x. Baseline jitter from iTime between beats.
// Renders corruption over a neon geometric background pattern.

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

#define PI 3.14159265359

// --- Self-contained hash utilities (no #include) ---

float hash(float n) {
    return fract(sin(n) * 43758.5453123);
}

float hash2(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

vec2 hash2v(vec2 p) {
    return vec2(
        fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453),
        fract(sin(dot(p, vec2(269.5, 183.3))) * 43758.5453)
    );
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

// --- Underlying scene: neon geometric shapes on dark gradient ---

vec3 background(vec2 uv) {
    // Dark gradient base
    vec3 bg = mix(vec3(0.02, 0.01, 0.05), vec3(0.05, 0.02, 0.08), uv.y);

    // Neon grid lines
    vec2 grid = fract(uv * 8.0);
    float lineX = smoothstep(0.02, 0.0, abs(grid.x - 0.5) - 0.48);
    float lineY = smoothstep(0.02, 0.0, abs(grid.y - 0.5) - 0.48);
    bg += vec3(0.0, 0.3, 0.5) * (lineX + lineY) * 0.15;

    // Diagonal neon stripes (animated)
    float stripe = sin((uv.x + uv.y) * 20.0 + iTime * 2.0);
    stripe = smoothstep(0.9, 1.0, stripe);
    bg += vec3(0.8, 0.1, 0.6) * stripe * 0.2;

    // Pulsing circles (iBandEnergy[4] drives radius)
    vec2 center = uv - 0.5;
    center.x *= iResolution.x / iResolution.y;
    float dist = length(center);
    float ring1 = abs(dist - 0.2 - iBandEnergy[4] * 0.1);
    float ring2 = abs(dist - 0.35 - iBandEnergy[5] * 0.08);
    bg += vec3(0.2, 0.9, 0.4) * smoothstep(0.01, 0.0, ring1 - 0.005) * 0.5;
    bg += vec3(0.9, 0.3, 0.1) * smoothstep(0.01, 0.0, ring2 - 0.005) * 0.4;

    // Horizontal scan bar (continuous motion from iTime — Req 8 AC 1)
    float scanBar = fract(iTime * 0.3);
    float scanDist = abs(uv.y - scanBar);
    bg += vec3(0.5, 0.8, 1.0) * smoothstep(0.02, 0.0, scanDist) * 0.3;

    return bg;
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // Beat multiplier: all distortion multiplied by (1.0 + iBeat * 3.0) = up to 4x
    // (Req 7 AC 1 — structural change on beat)
    float beatMult = 1.0 + iBeat * 3.0;

    // Baseline jitter from iTime (Req 8 AC 1 — continuous motion even with zero audio)
    float baseJitter = noise(vec2(iTime * 1.7, 0.0)) * 0.01;

    // --- Block displacement (8x8 grid) ---
    // Uses iBandEnergy[0] (sub-bass) for displacement magnitude
    // Uses iBandEnergy[3] (mid) for vertical flip probability
    vec2 blockCoord = floor(uv * 8.0);
    float blockHash = hash2(blockCoord + floor(iTime * 4.0));

    // Displacement magnitude: baseline + audio + beat amplification
    float displaceMag = (baseJitter + iBandEnergy[0] * 0.04 + iBandEnergy[3] * 0.02) * beatMult;

    // Only displace some blocks (hash threshold)
    float displaceThreshold = 0.6 - iBeat * 0.3; // more blocks displace on beat
    vec2 blockOffset = vec2(0.0);
    if (blockHash > displaceThreshold) {
        // Horizontal shift based on block hash
        float shiftDir = hash2(blockCoord + vec2(7.3, 2.1)) * 2.0 - 1.0;
        blockOffset.x = shiftDir * displaceMag;

        // Vertical flip for some blocks (iBandEnergy[3] influence)
        float flipChance = hash2(blockCoord + vec2(13.7, 5.9));
        if (flipChance > 0.7 - iBandEnergy[3] * 0.2) {
            // Flip UV.y within the block
            vec2 blockFract = fract(uv * 8.0);
            blockOffset.y = (1.0 - 2.0 * blockFract.y) / 8.0 * displaceMag * 10.0;
        }
    }

    vec2 displacedUV = uv + blockOffset;

    // --- RGB channel splitting (chromatic aberration) ---
    // Offset driven by iBandEnergy[0] (sub-bass) — Req 7 AC 3 (band 0)
    float chromaBase = iBandEnergy[0] * 0.05;
    // iBandEnergy[6] (brilliance) adds high-frequency shimmer — Req 7 AC 3 (band 6)
    float chromaShimmer = iBandEnergy[6] * 0.02;
    float chromaOffset = (chromaBase + chromaShimmer + baseJitter) * beatMult;

    // Direction of chromatic split varies over time
    float chromaAngle = iTime * 0.5 + iBandEnergy[2] * PI;
    vec2 chromaDir = vec2(cos(chromaAngle), sin(chromaAngle)) * chromaOffset;

    // Sample RGB at different offsets
    vec2 uvR = displacedUV + chromaDir;
    vec2 uvG = displacedUV;
    vec2 uvB = displacedUV - chromaDir;

    // Clamp UVs to valid range
    uvR = clamp(uvR, 0.0, 1.0);
    uvG = clamp(uvG, 0.0, 1.0);
    uvB = clamp(uvB, 0.0, 1.0);

    // Sample the background at each channel offset
    float r = background(uvR).r;
    float g = background(uvG).g;
    float b = background(uvB).b;

    vec3 col = vec3(r, g, b);

    // --- Scanline noise flicker ---
    // Horizontal stripes flicker based on hash(floor(uv.y * 100) + iTime)
    // Uses iBandEnergy[2] (low-mid) for scanline intensity — Req 7 AC 3 (band 2)
    float scanlineY = floor(uv.y * 100.0);
    float scanlineHash = hash(scanlineY + floor(iTime * 12.0));

    // Scanline intensity: baseline + audio + beat
    float scanlineIntensity = (0.05 + iBandEnergy[2] * 0.3) * beatMult;

    // Apply scanline noise (darken some lines, brighten others)
    float scanlineEffect = (scanlineHash - 0.5) * scanlineIntensity;
    col += scanlineEffect;

    // Heavy scanlines: periodic darkening
    float scanline = sin(uv.y * iResolution.y * PI) * 0.5 + 0.5;
    col *= 0.92 + 0.08 * scanline;

    // --- Additional distortion: horizontal tear lines ---
    // Random horizontal tears that shift pixels (more on beat)
    float tearY = floor(uv.y * 40.0);
    float tearHash = hash(tearY + floor(iTime * 8.0) + 99.0);
    float tearThreshold = 0.92 - iBeat * 0.3;
    if (tearHash > tearThreshold) {
        float tearShift = (hash(tearY + iTime) - 0.5) * 0.05 * beatMult;
        vec2 tearUV = vec2(uv.x + tearShift, uv.y);
        tearUV = clamp(tearUV, 0.0, 1.0);
        col = mix(col, background(tearUV), 0.7);
    }

    // --- White noise burst on strong beats ---
    if (iBeat > 0.8) {
        float noiseVal = hash2(uv * iResolution + iTime * 100.0);
        col = mix(col, vec3(noiseVal), iBeat * 0.15);
    }

    // --- Glow intensity modulation ---
    col *= 1.0 + iGlowIntensity * 0.3;

    // --- Background opacity ---
    col *= iBgOpacity;

    // Req 7 AC 2: While iBeat > 0.5, multiple parameters differ from resting state:
    //   1. Block displacement magnitude is amplified (displaceMag * beatMult)
    //   2. Chromatic aberration offset is amplified (chromaOffset * beatMult)
    //   3. Scanline intensity is amplified (scanlineIntensity * beatMult)
    //   4. More blocks are displaced (lower displaceThreshold)
    //   5. More tear lines appear (lower tearThreshold)

    // Req 7 AC 3: Uses iBandEnergy[0] (chromatic + displacement),
    //   iBandEnergy[2] (scanline + chroma angle), iBandEnergy[3] (block displacement + flip),
    //   iBandEnergy[4..6] (background rings + shimmer) — well over 3 bands

    // Req 8 AC 1: iTime drives baseJitter noise, scanline hash evolution,
    //   background scan bar, diagonal stripes, block hash evolution, chromatic angle
    //   — all produce frame-to-frame change even with zero audio input

    fragColor = vec4(col, 1.0);
}
