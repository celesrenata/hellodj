#version 330 core

// Nebula — Fractal Brownian Motion (FBM) volumetric fog with slow rotation.
// 4-octave FBM noise sampled at multiple scales. FFT texture drives local
// cloud density. Deep blues/purples with star-like point highlights.
// Beat triggers brightness surge. iBandEnergy[1..3] modulates movement speed.

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

// FBM capped at 4 octaves (Req 9 AC 2: performance constraint)
float fbm(vec2 p) {
    float value = 0.0;
    float amplitude = 0.5;
    float frequency = 1.0;
    for (int i = 0; i < 4; i++) {
        value += amplitude * noise(p * frequency);
        frequency *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

mat2 rotate2d(float angle) {
    float c = cos(angle);
    float s = sin(angle);
    return mat2(c, -s, s, c);
}

void main() {
    vec2 uv = vUV;
    float aspect = iResolution.x / iResolution.y;

    // Center coordinates with aspect correction
    vec2 center = (uv - 0.5) * 2.0;
    center.x *= aspect;

    // iBandEnergy[1..3] modulates cloud movement speed (Req 7 AC 3: 3 distinct bands)
    float bandSpeed1 = iBandEnergy[1];
    float bandSpeed2 = iBandEnergy[2];
    float bandSpeed3 = iBandEnergy[3];
    float movementSpeed = 1.0 + bandSpeed1 * 1.5 + bandSpeed2 * 1.0 + bandSpeed3 * 0.7;

    // Slow rotation: UV rotated by iTime * 0.02 (Req 8 AC 1: continuous motion)
    vec2 rotated = rotate2d(iTime * 0.02) * center;

    // Time-based drift for continuous animation even without audio
    float drift = iTime * 0.05 * movementSpeed;

    // --- Multi-scale FBM cloud layers ---

    // Large-scale nebula structure
    vec2 p1 = rotated * 1.5 + vec2(drift * 0.3, drift * 0.2);
    float cloud1 = fbm(p1);

    // Medium-scale detail with different drift direction
    vec2 p2 = rotated * 3.0 + vec2(-drift * 0.4, drift * 0.5) + vec2(cloud1 * 0.3);
    float cloud2 = fbm(p2);

    // Fine-scale wisps — offset by coarser layers for organic look
    vec2 p3 = rotated * 5.0 + vec2(drift * 0.6, -drift * 0.35) + vec2(cloud2 * 0.2, cloud1 * 0.2);
    float cloud3 = fbm(p3);

    // Combine cloud layers into base density
    float density = cloud1 * 0.5 + cloud2 * 0.3 + cloud3 * 0.2;

    // FFT texture drives local cloud density (design spec: density += texture(iFFT, uv.x).r * 0.3)
    float fftVal = texture(iFFT, uv.x).r;
    density += fftVal * 0.3;

    // Remap density to useful range [0, 1]
    density = clamp(density, 0.0, 1.0);

    // --- Color: deep blues/purples with variation ---

    // Base color from density — deep blue to purple gradient
    vec3 deepBlue = vec3(0.02, 0.03, 0.15);
    vec3 midPurple = vec3(0.15, 0.05, 0.35);
    vec3 brightPurple = vec3(0.4, 0.15, 0.6);
    vec3 hotBlue = vec3(0.2, 0.4, 0.9);

    // Multi-stop color ramp based on density
    vec3 nebulaColor;
    if (density < 0.3) {
        nebulaColor = mix(deepBlue, midPurple, density / 0.3);
    } else if (density < 0.6) {
        nebulaColor = mix(midPurple, brightPurple, (density - 0.3) / 0.3);
    } else {
        nebulaColor = mix(brightPurple, hotBlue, (density - 0.6) / 0.4);
    }

    // Add subtle color variation from noise to break up uniformity
    float colorVar = noise(rotated * 2.0 + iTime * 0.01);
    nebulaColor = mix(nebulaColor, nebulaColor * vec3(0.8, 1.2, 1.3), colorVar * 0.3);

    // Scale brightness by density (denser regions glow brighter)
    float brightness = 0.4 + density * 1.2;

    // --- Star highlights ---

    // Bright star-like point highlights using high-frequency hash
    float stars = 0.0;
    vec2 starUV = uv * 50.0; // grid for star placement
    vec2 starCell = floor(starUV);
    vec2 starFract = fract(starUV);

    // Check neighboring cells for star centers
    for (int sx = -1; sx <= 1; sx++) {
        for (int sy = -1; sy <= 1; sy++) {
            vec2 neighbor = vec2(float(sx), float(sy));
            vec2 cellId = starCell + neighbor;
            // Deterministic random star position within cell
            vec2 starPos = vec2(hash(cellId), hash(cellId + vec2(31.7, 47.3)));
            // Only some cells have stars (sparse)
            float starPresence = step(0.92, hash(cellId + vec2(113.1, 271.5)));
            // Distance from fragment to star center
            float d = length(starFract - neighbor - starPos);
            // Sharp point glow
            float starGlow = starPresence * 0.003 / (d * d + 0.003);
            // Twinkle based on time + cell hash
            float twinkle = 0.5 + 0.5 * sin(iTime * (2.0 + hash(cellId) * 4.0) + hash(cellId) * 6.28);
            stars += starGlow * twinkle;
        }
    }

    // Stars brighter in low-density (dark) regions
    float starMask = 1.0 - smoothstep(0.2, 0.5, density);
    vec3 starColor = vec3(0.8, 0.85, 1.0) * stars * starMask;

    // --- Beat response ---

    // Beat triggers brightness surge (Req 7 AC 1: structural change)
    brightness *= 1.0 + iBeat * 0.4;

    // Peak state: while iBeat > 0.5, cloud density boost + color shift (Req 7 AC 2: 2+ parameters differ)
    float peakFactor = smoothstep(0.5, 1.0, iBeat);
    brightness += peakFactor * 0.5;                              // param 1: brightness boost
    nebulaColor = mix(nebulaColor, hotBlue, peakFactor * 0.3);   // param 2: color shift toward hot blue

    // --- Compose final color ---

    vec3 col = nebulaColor * brightness + starColor;

    // Radial vignette for depth
    float vignette = 1.0 - smoothstep(0.5, 1.8, length(center));
    col *= (0.7 + vignette * 0.3);

    // Glow intensity uniform
    col *= (0.8 + iGlowIntensity * 0.4);

    // Background opacity
    col *= iBgOpacity;

    fragColor = vec4(col, 1.0);
}
