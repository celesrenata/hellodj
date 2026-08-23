#version 330 core

// Reflective metallic surface with spectrum displacement.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

vec3 envMap(vec3 rd) {
    float t = iTime * 0.2;
    float y = rd.y * 0.5 + 0.5;
    vec3 sky = mix(vec3(0.1, 0.0, 0.2), vec3(0.0, 0.1, 0.3), y);
    // Fake environment highlights
    float highlight = pow(max(dot(rd, normalize(vec3(1.0, 1.0, -0.5))), 0.0), 16.0);
    sky += vec3(1.0, 0.8, 0.6) * highlight;
    return sky;
}

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[2] + iBandEnergy[3];
    float high = iBandEnergy[5] + iBandEnergy[6];

    // Surface displacement driven by spectrum bands
    float displacement = 0.0;
    displacement += sin(p.x * 4.0 + iTime * 1.5) * bass * 0.3;
    displacement += sin(p.y * 6.0 - iTime * 1.2) * mid * 0.2;
    displacement += sin((p.x + p.y) * 8.0 + iTime * 2.0) * high * 0.15;
    displacement += sin(length(p) * 5.0 - iTime * 3.0) * iBeat * 0.4;

    // Compute surface normal from displacement
    float eps = 0.01;
    float dx = displacement - (
        sin((p.x + eps) * 4.0 + iTime * 1.5) * bass * 0.3 +
        sin(p.y * 6.0 - iTime * 1.2) * mid * 0.2 +
        sin((p.x + eps + p.y) * 8.0 + iTime * 2.0) * high * 0.15 +
        sin(length(p + vec2(eps, 0.0)) * 5.0 - iTime * 3.0) * iBeat * 0.4
    );
    float dy = displacement - (
        sin(p.x * 4.0 + iTime * 1.5) * bass * 0.3 +
        sin((p.y + eps) * 6.0 - iTime * 1.2) * mid * 0.2 +
        sin((p.x + p.y + eps) * 8.0 + iTime * 2.0) * high * 0.15 +
        sin(length(p + vec2(0.0, eps)) * 5.0 - iTime * 3.0) * iBeat * 0.4
    );

    vec3 normal = normalize(vec3(dx * 20.0, dy * 20.0, 1.0));

    // View direction
    vec3 viewDir = normalize(vec3(p, 1.5));
    vec3 reflected = reflect(viewDir, normal);

    // Environment reflection
    vec3 envColor = envMap(reflected);

    // Metallic coloring (chrome with subtle color shift)
    float fresnel = pow(1.0 - abs(dot(viewDir, normal)), 3.0);
    vec3 metalColor = mix(vec3(0.8, 0.85, 0.9), vec3(0.3, 0.5, 0.8), fresnel);

    vec3 color = envColor * metalColor;

    // Specular highlights
    float spec = pow(max(reflected.z, 0.0), 64.0);
    color += vec3(1.0) * spec * 0.5;

    // Beat-reactive brightness
    color *= 1.0 + iBeat * 0.3;

    fragColor = vec4(color, 1.0);
}
