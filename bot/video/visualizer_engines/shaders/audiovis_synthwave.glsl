#version 330 core

// Synthwave — Perspective grid floor scrolling forward via iTime.
// 1D noise mountain silhouette at horizon with height from iBandEnergy[0..1].
// Sunset gradient (orange→magenta→purple). Beat triggers horizon flash.
// Sun circle pulsing with bass. (Req 6 AC 1, Req 7 AC 1-3, Req 8 AC 1)

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

float hash(float p) {
    return fract(sin(p * 127.1) * 43758.5453123);
}

float hash2(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

// 1D value noise for mountain silhouette
float noise1D(float x) {
    float i = floor(x);
    float f = fract(x);
    f = f * f * (3.0 - 2.0 * f); // smoothstep
    return mix(hash(i), hash(i + 1.0), f);
}

// FBM for mountain detail
float mountainNoise(float x, int octaves) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < 4; i++) {
        if (i >= octaves) break;
        value += amplitude * noise1D(x * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // Horizon line position (40% from bottom)
    float horizon = 0.4;

    // --- Sky region (above horizon) ---
    vec3 col = vec3(0.0);

    if (uv.y >= horizon) {
        // Normalized sky coordinate [0, 1] from horizon to top
        float skyT = (uv.y - horizon) / (1.0 - horizon);

        // Sunset gradient: orange at horizon → magenta → purple → dark at top
        vec3 orange  = vec3(1.0, 0.4, 0.05);
        vec3 magenta = vec3(0.85, 0.1, 0.5);
        vec3 purple  = vec3(0.3, 0.05, 0.4);
        vec3 dark    = vec3(0.05, 0.01, 0.12);

        vec3 skyCol;
        if (skyT < 0.25) {
            skyCol = mix(orange, magenta, skyT / 0.25);
        } else if (skyT < 0.55) {
            skyCol = mix(magenta, purple, (skyT - 0.25) / 0.3);
        } else {
            skyCol = mix(purple, dark, (skyT - 0.55) / 0.45);
        }

        // --- Sun circle at horizon, pulsing with bass (iBandEnergy[0]) ---
        vec2 sunCenter = vec2(0.5, horizon + 0.18);
        vec2 sunUV = uv - sunCenter;
        sunUV.x *= aspect;
        float sunDist = length(sunUV);

        // Sun radius pulses with bass energy
        float sunBaseRadius = 0.12;
        float sunRadius = sunBaseRadius + iBandEnergy[0] * 0.04;
        float sunGlow = smoothstep(sunRadius + 0.02, sunRadius - 0.01, sunDist);

        // Sun horizontal bands (classic synthwave sun slicing)
        float bands = step(0.0, sin((uv.y - sunCenter.y) * 80.0 - iTime * 2.0));
        float sunAlpha = sunGlow * (0.7 + 0.3 * bands);

        vec3 sunColor = mix(vec3(1.0, 0.8, 0.1), vec3(1.0, 0.3, 0.1),
                           (sunCenter.y - uv.y + sunRadius) / (2.0 * sunRadius));
        skyCol = mix(skyCol, sunColor, sunAlpha);

        // --- Mountains: 1D noise silhouette at horizon ---
        // Height driven by iBandEnergy[0] and iBandEnergy[1] (Req 7 AC 3)
        float mountainX = (uv.x - 0.5) * 4.0;
        float mountainHeight = mountainNoise(mountainX * 1.5 + 0.5, 4) * 0.12
                             + mountainNoise(mountainX * 3.0 + 2.7, 3) * 0.06;

        // Modulate height with bass energy bands
        mountainHeight *= (0.6 + iBandEnergy[0] * 0.8 + iBandEnergy[1] * 0.6);

        // Mountain silhouette mask
        float mountainLine = horizon + mountainHeight;
        if (uv.y < mountainLine) {
            // Dark silhouette
            skyCol = vec3(0.02, 0.0, 0.05);

            // Mountain edge glow (purple outline at top edge)
            float edgeDist = abs(uv.y - mountainLine);
            float edgeGlow = exp(-edgeDist * 80.0) * 0.6;
            skyCol += vec3(0.5, 0.1, 0.8) * edgeGlow;
        }

        // --- Beat-triggered horizon flash (Req 7 AC 1 — structural change) ---
        // White glow at vanishing point that decays (beat impulse acts as trigger)
        float flashIntensity = iBeat * iBeat; // quadratic decay for sharp attack
        float flashDist = length(vec2((uv.x - 0.5) * aspect, uv.y - horizon));
        float flash = flashIntensity * exp(-flashDist * 4.0) * 2.0;
        skyCol += vec3(1.0, 0.95, 0.9) * flash;

        col = skyCol;
    }

    // --- Ground region (below horizon): Perspective grid floor ---
    if (uv.y < horizon) {
        // Perspective projection: map screen Y to world Z distance
        float groundT = (horizon - uv.y) / horizon; // 0 at horizon, 1 at bottom
        float depth = 0.1 / (groundT + 0.001); // perspective depth (farther = larger value)

        // World X coordinate (perspective-corrected)
        float worldX = (uv.x - 0.5) * depth * 2.0;

        // Grid scroll forward continuously via iTime (Req 8 AC 1)
        float zOffset = iTime * 0.5;
        float worldZ = depth + zOffset;

        // Grid lines
        float gridX = abs(fract(worldX * 0.5) - 0.5);
        float gridZ = abs(fract(worldZ * 0.3) - 0.5);

        // Line thickness (thinner lines further away for perspective feel)
        float lineWidth = 0.02 + groundT * 0.03;
        float lineX = smoothstep(lineWidth, 0.0, gridX);
        float lineZ = smoothstep(lineWidth, 0.0, gridZ);
        float grid = max(lineX, lineZ);

        // Grid color: neon cyan/magenta
        vec3 gridColor = mix(vec3(0.1, 0.6, 1.0), vec3(0.9, 0.2, 0.8),
                            sin(worldZ * 0.2 + iTime * 0.3) * 0.5 + 0.5);

        // Modulate grid brightness with iBandEnergy[3] (mid-frequency) — 3rd distinct band
        float gridBrightness = 0.5 + iBandEnergy[3] * 0.5;
        gridColor *= gridBrightness;

        // Distance fog (fade grid near horizon)
        float fog = exp(-groundT * 0.3) * 0.7 + 0.3;

        // Base ground color (dark purple)
        vec3 groundCol = vec3(0.03, 0.01, 0.08);
        col = mix(groundCol, gridColor, grid * fog);

        // Horizon glow on ground (reflected sunset)
        float horizonReflect = exp(-groundT * 8.0) * 0.3;
        col += vec3(0.8, 0.2, 0.5) * horizonReflect;

        // Beat flash reflects on ground too (Req 7 AC 2 — 2+ parameters differ)
        float groundFlash = iBeat * exp(-groundT * 3.0) * 0.5;
        col += vec3(1.0, 0.9, 0.8) * groundFlash;

        // iBandEnergy[5] (presence) adds sparkle to grid intersections — 4th distinct band
        float intersection = lineX * lineZ;
        col += vec3(0.6, 0.8, 1.0) * intersection * iBandEnergy[5] * 1.5;
    }

    // --- Peak state (Req 7 AC 2): while iBeat > 0.5, 2+ parameters differ ---
    // Parameter 1: overall brightness boost
    // Parameter 2: color saturation increase
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    col *= 1.0 + peakFactor * 0.8;
    // Desaturate-then-hyper-saturate for peak
    float lum = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(col, col * vec3(1.2, 0.9, 1.4), peakFactor * 0.5);

    // Glow intensity uniform modulation
    col *= 0.7 + iGlowIntensity * 0.3;

    // Background opacity
    col *= iBgOpacity;

    // Band usage summary (Req 7 AC 3 — minimum 3 distinct bands):
    // iBandEnergy[0] — sun pulse + mountain height
    // iBandEnergy[1] — mountain height secondary
    // iBandEnergy[3] — grid brightness
    // iBandEnergy[5] — grid intersection sparkle

    fragColor = vec4(col, 1.0);
}
