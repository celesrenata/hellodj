#version 330 core

// Aurora — Layered FBM noise curtains (northern lights).
// 4 vertical curtain layers with additive blending.
// Amplitude from iBandEnergy[2..4]. Slow vertical drift via iTime.
// Green→cyan→purple color spectrum. Beat triggers brightness surge
// + horizontal wave acceleration. Continuous animation from iTime alone.

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

// --- Self-contained noise utilities ---

float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
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

// FBM capped at 4 octaves (Req 9 performance constraint)
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

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // Slow vertical drift from iTime (Req 8 AC 1: continuous animation without audio)
    float drift = iTime * 0.1;

    // Beat-driven horizontal wave acceleration (Req 7 AC 2: 2nd parameter differs)
    float waveSpeed = 1.0 + iBeat * 3.0;

    // Beat brightness surge (Req 7 AC 1: structural brightness change)
    float beatSurge = 1.0 + iBeat * 2.5;

    // Sum of all band energies for color spectrum shift (Req 7 AC 3: uses bands 0-6)
    float totalEnergy = 0.0;
    for (int i = 0; i < 7; i++) {
        totalEnergy += iBandEnergy[i];
    }

    // Curtain amplitude driven by iBandEnergy[2..4] (Req 7 AC 3: 3 distinct bands)
    float amp2 = iBandEnergy[2];
    float amp3 = iBandEnergy[3];
    float amp4 = iBandEnergy[4];

    // Accumulated color from additive layer blending
    vec3 col = vec3(0.0);

    // 4 curtain layers with different offsets and frequencies
    for (int layer = 0; layer < 4; layer++) {
        float fl = float(layer);

        // Each layer uses a different seed offset
        vec2 seed = vec2(fl * 7.3, fl * 3.1);

        // Horizontal wave position — iTime provides base motion, beat accelerates
        float hWave = iTime * (0.08 + fl * 0.03) * waveSpeed;

        // FBM noise for curtain wave shape
        // X coordinate drives horizontal variation, Y drives vertical drift
        vec2 noiseCoord = vec2(
            uv.x * (2.0 + fl * 0.5) * aspect + hWave,
            drift + fl * 1.7
        ) + seed;

        float curtainNoise = fbm(noiseCoord, 4);

        // Curtain vertical position — each layer at a different base height
        float baseY = 0.55 + fl * 0.08;

        // Amplitude modulation from iBandEnergy[2..4] — each layer mixes differently
        float ampMod = 0.15 + 0.3 * mix(
            mix(amp2, amp3, fl / 3.0),
            amp4,
            fl / 4.0
        );

        // Curtain wave displacement
        float wave = (curtainNoise - 0.5) * ampMod;

        // Vertical distance from curtain center — creates the band shape
        float curtainCenter = baseY + wave;
        float dist = abs(uv.y - curtainCenter);

        // Curtain thickness — thinner at edges, brighter at center
        float thickness = 0.04 + 0.03 * (1.0 + amp3);
        float curtainAlpha = smoothstep(thickness, 0.0, dist);

        // Secondary glow layer for softer falloff
        float glow = smoothstep(thickness * 3.0, 0.0, dist) * 0.3;

        // Green → cyan → purple spectrum
        // Layer index + noise + total energy shifts the hue
        float hueShift = fl * 0.25 + curtainNoise * 0.3 + totalEnergy * 0.1;

        // Base green, shift toward cyan then purple
        vec3 layerColor;
        float t = fract(hueShift + iTime * 0.02);
        if (t < 0.33) {
            // Green to cyan
            layerColor = mix(vec3(0.1, 0.9, 0.3), vec3(0.1, 0.8, 0.9), t * 3.0);
        } else if (t < 0.66) {
            // Cyan to purple
            layerColor = mix(vec3(0.1, 0.8, 0.9), vec3(0.6, 0.2, 0.9), (t - 0.33) * 3.0);
        } else {
            // Purple back to green
            layerColor = mix(vec3(0.6, 0.2, 0.9), vec3(0.1, 0.9, 0.3), (t - 0.66) * 3.0);
        }

        // Per-layer intensity variation
        float layerIntensity = 0.6 + 0.4 * noise(vec2(fl * 5.0, iTime * 0.1));

        // Additive blending between layers
        col += layerColor * (curtainAlpha + glow) * layerIntensity;
    }

    // Apply beat brightness surge (Req 7 AC 1: structural brightness change)
    col *= beatSurge;

    // Subtle background — dark with faint green wash from aurora reflection
    float bgGlow = smoothstep(1.0, 0.3, uv.y) * 0.05;
    col += vec3(0.02, 0.06, 0.04) * bgGlow;

    // Glow intensity uniform
    col *= (0.8 + iGlowIntensity * 0.4);

    // Background opacity blend
    col *= iBgOpacity;

    fragColor = vec4(col, 1.0);
}
