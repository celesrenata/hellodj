#version 330 core

// Raymarched floating orbs with beat-driven deformation.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

float sdSphere(vec3 p, float r) {
    return length(p) - r;
}

float scene(vec3 p) {
    float bass = iBandEnergy[0] + iBandEnergy[1];

    // Multiple orbs
    float d = 1e10;
    for (int i = 0; i < 5; i++) {
        float fi = float(i);
        vec3 offset = vec3(
            sin(iTime * 0.5 + fi * 1.3) * 2.0,
            cos(iTime * 0.4 + fi * 0.9) * 1.5,
            sin(iTime * 0.3 + fi * 2.1) * 1.0
        );
        float radius = 0.4 + 0.1 * sin(fi * 2.0);

        // Beat deformation
        float deform = sin(p.x * 3.0 + iTime) * sin(p.y * 3.0) * iBeat * 0.3;
        radius += deform;

        // Bass inflation
        radius += bass * 0.15;

        d = min(d, sdSphere(p - offset, radius));
    }

    return d;
}

vec3 getNormal(vec3 p) {
    vec2 e = vec2(0.001, 0.0);
    return normalize(vec3(
        scene(p + e.xyy) - scene(p - e.xyy),
        scene(p + e.yxy) - scene(p - e.yxy),
        scene(p + e.yyx) - scene(p - e.yyx)
    ));
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    // Camera
    vec3 ro = vec3(0.0, 0.0, -5.0);
    vec3 rd = normalize(vec3(p, 1.5));

    // Raymarch
    float t = 0.0;
    float hit = 0.0;
    for (int i = 0; i < 64; i++) {
        vec3 pos = ro + rd * t;
        float d = scene(pos);
        if (d < 0.001) { hit = 1.0; break; }
        if (t > 20.0) break;
        t += d;
    }

    vec3 color = vec3(0.02, 0.01, 0.05); // dark background

    if (hit > 0.5) {
        vec3 pos = ro + rd * t;
        vec3 nor = getNormal(pos);

        // Lighting
        vec3 lightDir = normalize(vec3(1.0, 1.0, -1.0));
        float diff = max(dot(nor, lightDir), 0.0);
        float spec = pow(max(dot(reflect(-lightDir, nor), -rd), 0.0), 32.0);

        // Audio-reactive iridescent coloring
        float mid = iBandEnergy[3];
        float hue = dot(nor, vec3(1.0)) * 2.0 + iTime * 0.3 + mid;
        vec3 baseColor = 0.5 + 0.5 * cos(hue + vec3(0.0, 2.094, 4.189));

        color = baseColor * diff + vec3(1.0) * spec * 0.5;
        color += baseColor * 0.1; // ambient
        color *= 1.0 + iBeat * 0.4;
    }

    // Fog
    color = mix(color, vec3(0.02, 0.01, 0.05), 1.0 - exp(-t * 0.1));

    fragColor = vec4(color, 1.0);
}
