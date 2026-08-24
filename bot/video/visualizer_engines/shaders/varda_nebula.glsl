#version 330 core

// Varda: Nebula / cosmic cloud — layered noise volumes with audio-driven flow.
// Deep space aesthetic with swirling gas clouds and starbursts.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

// 2D rotation
mat2 rot(float a) { return mat2(cos(a), -sin(a), sin(a), cos(a)); }

// Simplex-ish noise (3D)
float hash(vec3 p) {
    p = fract(p * vec3(0.1031, 0.1030, 0.0973));
    p += dot(p, p.yxz + 33.33);
    return fract((p.x + p.y) * p.z);
}

float noise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);

    float n000 = hash(i);
    float n100 = hash(i + vec3(1, 0, 0));
    float n010 = hash(i + vec3(0, 1, 0));
    float n110 = hash(i + vec3(1, 1, 0));
    float n001 = hash(i + vec3(0, 0, 1));
    float n101 = hash(i + vec3(1, 0, 1));
    float n011 = hash(i + vec3(0, 1, 1));
    float n111 = hash(i + vec3(1, 1, 1));

    float nx00 = mix(n000, n100, f.x);
    float nx10 = mix(n010, n110, f.x);
    float nx01 = mix(n001, n101, f.x);
    float nx11 = mix(n011, n111, f.x);

    float nxy0 = mix(nx00, nx10, f.y);
    float nxy1 = mix(nx01, nx11, f.y);

    return mix(nxy0, nxy1, f.z);
}

float fbm(vec3 p) {
    float f = 0.0;
    float amp = 0.5;
    for (int i = 0; i < 5; i++) {
        f += amp * noise(p);
        p *= 2.1;
        amp *= 0.5;
    }
    return f;
}

void main() {
    vec2 uv = fragCoord * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.15;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];
    float highs = iBandEnergy[5] + iBandEnergy[6];

    // Flow direction influenced by audio
    vec2 flow = vec2(
        sin(t * 2.0) * 0.3 + bass * 0.2,
        cos(t * 1.5) * 0.2 + mids * 0.1
    );

    // Layered nebula clouds
    vec3 col = vec3(0.0);

    // Layer 1: deep background (slow, large scale)
    vec3 p1 = vec3(uv * 1.5 + flow * 0.3, t * 0.5);
    float n1 = fbm(p1);
    col += vec3(0.1, 0.02, 0.15) * n1;

    // Layer 2: mid cloud (medium scale, brighter)
    vec3 p2 = vec3(uv * 3.0 + flow * 0.7, t * 0.8);
    p2.xy *= rot(t * 0.2);
    float n2 = fbm(p2);
    n2 = pow(n2, 1.5 + bass);  // sharpen with bass
    col += vec3(0.3, 0.05, 0.4) * n2 * (1.0 + bass * 0.5);

    // Layer 3: bright filaments (small scale, sharp)
    vec3 p3 = vec3(uv * 6.0 + flow, t * 1.2);
    p3.xy *= rot(-t * 0.3 + iBeat * 0.5);
    float n3 = fbm(p3);
    n3 = pow(n3, 2.0 + mids * 2.0);
    col += vec3(0.6, 0.2, 0.8) * n3 * (1.0 + mids);

    // Layer 4: hot gas (very fine, cyan/white)
    vec3 p4 = vec3(uv * 10.0 + flow * 1.5, t * 2.0);
    float n4 = fbm(p4);
    n4 = pow(n4, 3.0);
    col += vec3(0.2, 0.8, 0.9) * n4 * highs * 2.0;

    // Stars (tiny bright points)
    float starField = hash(vec3(floor(uv * 200.0), 1.0));
    if (starField > 0.997) {
        float twinkle = 0.5 + 0.5 * sin(iTime * 3.0 + starField * 100.0);
        col += vec3(1.0) * twinkle * 0.8;
    }

    // Beat burst — radial bright pulse from center
    float centerDist = length(uv);
    float burstRadius = iBeat * 2.0;
    float burst = smoothstep(burstRadius + 0.1, burstRadius, centerDist);
    burst *= iBeat;
    col += vec3(0.5, 0.2, 0.8) * burst * 2.0;

    // Tone mapping
    col = 1.0 - exp(-col * 2.0);

    // Subtle vignette
    float vig = 1.0 - length(fragCoord - 0.5) * 0.6;
    col *= vig;

    fragColor = vec4(col, 1.0);
}
