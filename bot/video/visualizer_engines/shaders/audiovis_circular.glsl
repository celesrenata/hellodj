#version 330 core

// Circular/radial spectrum fragment shader for AudioVis engine.
// Renders FFT as radial bars emanating from center with glow.

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
#define TAU 6.28318530718

void main() {
    vec2 uv = vUV;

    // Center coordinates [-1, 1] with aspect correction
    vec2 center = (uv - 0.5) * 2.0;
    center.x *= iResolution.x / iResolution.y;

    // Polar coordinates
    float radius = length(center);
    float angle = atan(center.y, center.x); // [-PI, PI]
    float normAngle = (angle + PI) / TAU;   // [0, 1]

    // Background — dark with subtle radial gradient
    vec3 bg = vec3(0.02, 0.02, 0.04) * iBgOpacity;
    bg += vec3(0.01, 0.005, 0.02) * (1.0 - radius * 0.5);

    // Inner ring radius (where bars start)
    float innerRadius = 0.2 + iBeat * 0.03;

    // Sample FFT at this angle
    float magnitude = texture(iFFT, normAngle).r;

    // Bar extends from innerRadius outward by magnitude
    float barLength = magnitude * 0.6;
    float outerRadius = innerRadius + barLength;

    // Check if pixel is within a bar
    float barAngleWidth = TAU / float(iFFTBins);
    float barAngle = mod(normAngle * TAU, barAngleWidth);
    float barGap = barAngleWidth * 0.2;
    bool inBarAngle = barAngle > barGap && barAngle < barAngleWidth - barGap;
    bool inBarRadius = radius > innerRadius && radius < outerRadius;

    vec3 col = bg;

    if (inBarAngle && inBarRadius) {
        // Color gradient along the bar (inner=blue, outer=pink)
        float barPos = (radius - innerRadius) / max(barLength, 0.001);
        vec3 barCol = mix(
            vec3(0.1, 0.3, 1.0),
            vec3(1.0, 0.2, 0.6),
            barPos
        );
        barCol *= 1.0 + iBeat * 0.5;

        // Brightness boost at tip
        float tipGlow = smoothstep(0.8, 1.0, barPos) * iGlowIntensity;
        barCol += vec3(0.5, 0.3, 0.8) * tipGlow;

        col = barCol;
    }

    // Inner circle glow (pulsing with beat)
    float innerGlow = smoothstep(innerRadius, innerRadius - 0.05, radius);
    vec3 centerColor = vec3(0.15, 0.1, 0.3) * (1.0 + iBeat * 1.5);
    col += centerColor * innerGlow;

    // Outer ring glow
    float ringDist = abs(radius - innerRadius);
    float ringGlow = 0.005 / (ringDist + 0.005);
    col += vec3(0.2, 0.1, 0.4) * ringGlow * 0.3;

    fragColor = vec4(col, 1.0);
}
