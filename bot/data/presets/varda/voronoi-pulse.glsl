#version 330 core

// Voronoi cells with bass-reactive cell size.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

vec2 hash2(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
    return fract(sin(p) * 43758.5453);
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[3] + iBandEnergy[4];

    // Cell scale driven by bass
    float scale = 4.0 + bass * 3.0;
    vec2 st = p * scale;
    vec2 i_st = floor(st);
    vec2 f_st = fract(st);

    float minDist = 1.0;
    vec2 minPoint = vec2(0.0);

    // 3x3 neighbor search
    for (int y = -1; y <= 1; y++) {
        for (int x = -1; x <= 1; x++) {
            vec2 neighbor = vec2(float(x), float(y));
            vec2 point = hash2(i_st + neighbor);
            // Animate points
            point = 0.5 + 0.5 * sin(iTime * 0.8 + 6.2831 * point);
            vec2 diff = neighbor + point - f_st;
            float dist = length(diff);
            if (dist < minDist) {
                minDist = dist;
                minPoint = point;
            }
        }
    }

    // Edge glow
    float edge = 1.0 - smoothstep(0.0, 0.05, minDist);
    edge *= 1.0 + iBeat * 3.0;

    // Color based on cell identity and audio
    float hue = minPoint.x * 6.28 + iTime * 0.2 + mid;
    vec3 cellColor = 0.5 + 0.5 * cos(hue + vec3(0.0, 2.094, 4.189));

    // Interior shading
    float interior = smoothstep(0.0, 0.6, minDist);
    vec3 color = cellColor * interior * 0.6;
    color += vec3(0.8, 0.9, 1.0) * edge;

    // Beat flash
    color += vec3(0.1, 0.05, 0.15) * iBeat;

    fragColor = vec4(color, 1.0);
}
