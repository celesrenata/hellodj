#version 330 core

// Storm — Branching FBM noise arcs (lightning simulation).
// 4-8 arc instances with random origins. Beat spawns new arc cluster.
// iBandEnergy[5..6] controls FBM octave count (branching complexity).
// Exponential glow falloff from arc distance. Dark cloud background.
// Arcs fade over time with exponential decay.

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

// --- Self-contained noise utilities ---

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

float hash1(float n) {
    return fract(sin(n) * 43758.5453123);
}

float noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash(i);
    float b = hash(i + vec2(1.0, 0.0));
    float c = hash(i + vec2(0.0, 1.0));
    float d = hash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// FBM with variable octave count (capped at 4 per Req 9 AC 2)
float fbm(vec2 p, int octaves) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < 4; i++) {
        if (i >= octaves) break;
        value += amplitude * noise(p * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

// --- Arc computation ---

// Compute distance from point to a single lightning arc.
// The arc runs vertically from origin, with FBM-displaced x position.
float arcDistance(vec2 uv, vec2 origin, float seed, float time, int octaves) {
    // Arc runs along the Y axis from origin upward/downward
    // Sample along Y to find closest point
    float minDist = 1e6;
    float arcLen = 0.8; // arc length in UV space

    // Direction from origin (randomized angle per seed)
    float angle = hash1(seed * 7.13) * PI * 2.0;
    vec2 dir = vec2(cos(angle), sin(angle));
    vec2 perp = vec2(-dir.y, dir.x);

    // Walk along the arc path and find minimum distance
    for (int s = 0; s < 16; s++) {
        float t = float(s) / 15.0;
        float along = t * arcLen;

        // FBM displacement perpendicular to arc direction
        float displacement = fbm(vec2(t * 4.0, time + seed), octaves) - 0.5;
        displacement *= 0.3; // scale displacement

        // Arc position
        vec2 arcPos = origin + dir * along + perp * displacement;

        // Distance from pixel to this arc sample
        float d = length(uv - arcPos);
        minDist = min(minDist, d);
    }

    return minDist;
}

// --- Background storm clouds ---

float stormClouds(vec2 uv, float time) {
    // Low-frequency noise layers for dark rolling clouds
    float clouds = 0.0;
    clouds += fbm(uv * 1.5 + time * 0.03, 4) * 0.5;
    clouds += fbm(uv * 3.0 - time * 0.02 + vec2(5.2, 1.3), 3) * 0.3;
    clouds += fbm(uv * 0.8 + time * 0.01 + vec2(1.7, 8.4), 3) * 0.2;
    return clouds;
}

void main() {
    vec2 uv = vUV;

    // Aspect-correct coordinates centered at origin
    vec2 coord = (uv - 0.5) * 2.0;
    coord.x *= iResolution.x / iResolution.y;

    // --- Background: dark storm clouds (Req 8 AC 1 — continuous from iTime) ---
    float clouds = stormClouds(coord, iTime);
    // Dark blue-gray storm palette
    vec3 bgColor = mix(
        vec3(0.02, 0.02, 0.04),  // near black
        vec3(0.06, 0.07, 0.12),  // dark navy
        clouds
    );
    // iBandEnergy[0] (sub-bass) adds subtle cloud brightness (Req 7 AC 3 — band 0)
    bgColor += vec3(0.01, 0.01, 0.03) * iBandEnergy[0];

    // --- Arc parameters ---

    // FBM octave count from iBandEnergy[5..6] (presence/brilliance): 2-4 octaves
    // (capped at 4 per performance constraint Req 9)
    float complexityRaw = iBandEnergy[5] * 0.6 + iBandEnergy[6] * 0.4;
    int octaves = 2 + int(clamp(complexityRaw * 3.0, 0.0, 2.0)); // 2-4

    // Arc count: 4 base + up to 4 more from beat (Req 7 AC 1 — beat spawns new cluster)
    int arcCount = 4 + int(iBeat * 4.0);

    // Beat cycle: determines which "generation" of arcs we're in
    // Each beat creates a new cluster; arcs fade with age
    float beatPeriod = 60.0 / max(iBPM, 60.0); // seconds per beat
    float timeInCycle = mod(iTime, beatPeriod * 4.0); // 4-beat cycle

    // --- Accumulate arc glow ---
    float totalGlow = 0.0;
    vec3 arcColor = vec3(0.0);

    for (int i = 0; i < 8; i++) {
        if (i >= arcCount) break;

        // Per-arc seed for random origin and characteristics
        float seed = float(i) * 1.618 + 0.5;

        // Arc origin: random position biased toward upper portion of screen
        // Beat shifts origin cluster (Req 7 AC 1 — structural change)
        float beatSeed = floor(iTime / beatPeriod);
        vec2 origin = vec2(
            hash1(seed * 3.17 + beatSeed) * 1.6 - 0.8,
            hash1(seed * 5.23 + beatSeed) * 0.8 - 0.1
        );

        // Arc age: time since this arc "spawned"
        // Each arc spawns at a slightly offset time within the beat cycle
        float spawnOffset = hash1(seed * 2.71) * beatPeriod;
        float age = mod(iTime - spawnOffset, beatPeriod * 2.0);

        // Fade over time: exponential decay (Arcs fade per design doc)
        float fade = exp(-age * 2.0);

        // Skip nearly-invisible arcs
        if (fade < 0.01) continue;

        // Compute distance to this arc
        float arcTime = iTime + seed * 10.0; // per-arc time offset for variety
        float dist = arcDistance(coord, origin, seed, arcTime, octaves);

        // Exponential glow falloff from arc distance
        float glow = exp(-dist * 40.0) * fade;

        // Arc color: electric blue/white with slight per-arc variation
        float hueShift = hash1(seed * 4.31) * 0.3;
        vec3 thisArcColor = mix(
            vec3(0.6, 0.7, 1.0),   // electric blue
            vec3(0.9, 0.8, 1.0),   // white-lavender
            hueShift
        );

        // Beat brightness boost (Req 7 AC 2 — 2+ params differ while iBeat > 0.5)
        float peakFactor = smoothstep(0.5, 1.0, iBeat);
        glow *= 1.0 + peakFactor * 2.0;  // brightness boost
        thisArcColor = mix(thisArcColor, vec3(1.0, 0.95, 1.0), peakFactor * 0.5); // whiten on beat

        totalGlow += glow;
        arcColor += thisArcColor * glow;
    }

    // Normalize arc color by total glow (prevent over-saturation)
    if (totalGlow > 0.001) {
        arcColor /= totalGlow;
    } else {
        arcColor = vec3(0.7, 0.8, 1.0);
    }

    // --- Compose final color ---

    // Arc contribution with glow intensity uniform
    float arcBrightness = min(totalGlow, 3.0) * iGlowIntensity;
    vec3 finalColor = bgColor + arcColor * arcBrightness;

    // Additional ambient flash on beat (whole-screen subtle flash)
    float flash = iBeat * 0.08 * iBandEnergy[0];
    finalColor += vec3(0.05, 0.06, 0.1) * flash;

    // FFT-driven subtle edge highlight (uses iFFT texture for extra reactivity)
    float fftSample = texture(iFFT, abs(coord.x) * 0.5 + 0.25).r;
    finalColor += vec3(0.02, 0.03, 0.06) * fftSample * iBandEnergy[3];

    // Apply background opacity
    finalColor *= iBgOpacity;

    // Req 7 AC 3 verified: uses iBandEnergy[0] (cloud brightness + flash),
    //   iBandEnergy[3] (FFT highlight), iBandEnergy[5] + iBandEnergy[6] (octave count)
    //   = 4 distinct bands (exceeds minimum 3)

    // Req 8 AC 1 verified: iTime drives cloud drift, arc paths (fbm time param),
    //   arc origins (beatSeed), and spawn timing — continuous evolution even with
    //   zero audio input.

    fragColor = vec4(finalColor, 1.0);
}
