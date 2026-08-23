#version 330 core

// 3D star field with bass-accelerated speed.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

float hash(vec3 p) {
    p = fract(p * vec3(0.1031, 0.1030, 0.0973));
    p += dot(p, p.yxz + 33.33);
    return fract((p.x + p.y) * p.z);
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float high = iBandEnergy[5] + iBandEnergy[6];

    // Speed controlled by bass
    float speed = iTime * (1.5 + bass * 4.0);

    vec3 color = vec3(0.0);

    // Multi-layer star field
    for (int layer = 0; layer < 4; layer++) {
        float fl = float(layer);
        float layerDepth = 1.0 + fl * 0.5;

        for (int i = 0; i < 30; i++) {
            float fi = float(i) + fl * 30.0;
            // Star position (pseudo-random, repeating along z)
            vec3 starPos = vec3(
                hash(vec3(fi, 0.0, fl)) * 2.0 - 1.0,
                hash(vec3(fi, 1.0, fl)) * 2.0 - 1.0,
                fract(hash(vec3(fi, 2.0, fl)) + speed * 0.1 / layerDepth)
            );

            // Project star
            float z = starPos.z * layerDepth;
            vec2 screenPos = starPos.xy / (z + 0.3);

            // Distance to star
            float dist = length(p - screenPos);

            // Star brightness (closer = brighter)
            float brightness = 0.003 / (dist * dist + 0.001);
            brightness *= (1.0 - z);  // fade with distance

            // Star color
            float starHue = hash(vec3(fi, 3.0, fl));
            vec3 starColor = mix(vec3(0.8, 0.9, 1.0), vec3(0.5, 0.7, 1.0), starHue);

            // Beat makes stars flicker
            brightness *= 1.0 + iBeat * 0.5 * step(0.9, hash(vec3(fi, 4.0, fl)));

            color += starColor * brightness;
        }
    }

    // High frequency shimmer
    color += vec3(0.1, 0.15, 0.2) * high * 0.3;

    // Subtle nebula background
    float nebula = sin(p.x * 3.0 + iTime * 0.1) * sin(p.y * 2.0 - iTime * 0.05);
    color += vec3(0.02, 0.01, 0.04) * (nebula * 0.5 + 0.5);

    fragColor = vec4(color, 1.0);
}
