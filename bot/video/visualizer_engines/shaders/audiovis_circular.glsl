#version 330 core

// Circular spectrum fragment shader for AudioVis engine.
// Renders FFT data as a radial bar/ring visualization.

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
#define TWO_PI 6.28318530718

void main() {
    // Center UV coordinates
    vec2 uv = vUV * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;  // aspect correction

    // Polar coordinates
    float r = length(uv);
    float angle = atan(uv.y, uv.x);  // -PI to PI
    float normAngle = (angle + PI) / TWO_PI;  // 0 to 1

    // Background
    vec3 bg = vec3(0.02, 0.01, 0.04) * iBgOpacity;

    // Ring parameters
    float innerRadius = 0.25 + iBeat * 0.03;
    float maxBarHeight = 0.35;

    // Sample FFT at this angle
    float fftVal = texture(iFFT, normAngle).r;

    // Beat pulse expands bars
    float barHeight = fftVal * maxBarHeight * (1.0 + iBeat * 0.3);
    float outerRadius = innerRadius + barHeight;

    vec3 col = bg;

    // Draw circular bars
    if (r > innerRadius && r < outerRadius) {
        float intensity = (r - innerRadius) / barHeight;

        // Color rotates with time
        float hueShift = iTime * 0.1;
        vec3 barCol = vec3(
            0.5 + 0.5 * sin(normAngle * TWO_PI + hueShift),
            0.5 + 0.5 * sin(normAngle * TWO_PI + hueShift + 2.094),
            0.5 + 0.5 * sin(normAngle * TWO_PI + hueShift + 4.189)
        );

        // Boost brightness with beat
        barCol *= 0.8 + iBeat * 0.5;
        barCol *= 1.0 - intensity * 0.3;

        col = barCol;

        // Glow at outer edge
        float edgeDist = outerRadius - r;
        if (edgeDist < 0.02 * iGlowIntensity) {
            col += vec3(0.3, 0.5, 1.0) * (1.0 - edgeDist / (0.02 * iGlowIntensity)) * iGlowIntensity * 0.5;
        }
    }

    // Inner ring glow
    float innerGlow = exp(-(r - innerRadius) * (r - innerRadius) / (0.001 * iGlowIntensity + 0.0001));
    col += vec3(0.2, 0.4, 0.8) * innerGlow * 0.3 * iGlowIntensity;

    // Center circle (dark with subtle pulse)
    if (r < innerRadius - 0.01) {
        float centerGlow = iBeat * 0.1 * (1.0 - r / innerRadius);
        col = bg + vec3(0.05, 0.1, 0.2) * centerGlow;
    }

    fragColor = vec4(col, 1.0);
}
