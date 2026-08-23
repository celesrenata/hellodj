#version 330 core

// Mirror-pattern kaleidoscope with FFT-driven rotation.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[2] + iBandEnergy[3];
    float high = iBandEnergy[5] + iBandEnergy[6];

    // FFT-driven rotation
    float rotSpeed = 0.3 + mid * 1.5;
    float angle = atan(p.y, p.x) + iTime * rotSpeed;
    float radius = length(p);

    // Kaleidoscope fold — 6 segments
    float segments = 6.0;
    angle = mod(angle, 6.28318 / segments);
    angle = abs(angle - 3.14159 / segments);

    // Map back to cartesian
    vec2 kp = vec2(cos(angle), sin(angle)) * radius;

    // Pattern generation
    float pattern = 0.0;
    pattern += sin(kp.x * 8.0 + iTime * 1.2) * 0.5;
    pattern += sin(kp.y * 6.0 - iTime * 0.8) * 0.3;
    pattern += sin((kp.x + kp.y) * 10.0 + iTime * 2.0) * 0.2 * bass;
    pattern += sin(radius * 12.0 - iTime * 3.0) * 0.3 * high;

    // Color palette
    float hue = pattern + iTime * 0.1;
    vec3 color = 0.5 + 0.5 * cos(hue * 3.0 + vec3(0.0, 2.094, 4.189));

    // Beat flash
    color *= 1.0 + iBeat * 0.8;

    // Center glow
    float glow = exp(-radius * 2.0) * bass;
    color += vec3(1.0, 0.8, 0.6) * glow;

    // Vignette
    color *= 1.0 - radius * 0.3;

    fragColor = vec4(color, 1.0);
}
