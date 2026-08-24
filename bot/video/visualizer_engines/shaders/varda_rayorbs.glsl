#version 330 core

// Varda: Raymarched metallic orbs with reflections and audio-reactive morphing.
// Sci-fi aesthetic with pulsing geometry.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

#define MAX_STEPS 64
#define MAX_DIST 20.0
#define SURF_DIST 0.001

float sdSphere(vec3 p, float r) { return length(p) - r; }

float sdBox(vec3 p, vec3 b) {
    vec3 q = abs(p) - b;
    return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0);
}

float smin(float a, float b, float k) {
    float h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

float scene(vec3 p) {
    float t = iTime * 0.5;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];

    // Central pulsing sphere
    float mainSphere = sdSphere(p, 0.8 + iBeat * 0.3 + bass * 0.2);

    // Orbiting smaller spheres (driven by band energy)
    float orbs = MAX_DIST;
    for (int i = 0; i < 5; i++) {
        float angle = t * (0.5 + float(i) * 0.3) + float(i) * 1.2566;
        float dist = 1.8 + sin(t + float(i)) * 0.3;
        float orbY = sin(angle * 0.7 + float(i)) * 0.8;
        vec3 orbPos = vec3(cos(angle) * dist, orbY, sin(angle) * dist);
        float radius = 0.25 + iBandEnergy[i] * 0.3;
        float orb = sdSphere(p - orbPos, radius);
        orbs = smin(orbs, orb, 0.4 + mids * 0.3);  // smooth merge on mids
    }

    // Smooth blend between center and orbs
    float d = smin(mainSphere, orbs, 0.5);

    // Floor plane (reflective ground)
    float floor = p.y + 2.0;
    d = min(d, floor);

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

float march(vec3 ro, vec3 rd) {
    float d = 0.0;
    for (int i = 0; i < MAX_STEPS; i++) {
        vec3 p = ro + rd * d;
        float ds = scene(p);
        d += ds;
        if (ds < SURF_DIST || d > MAX_DIST) break;
    }
    return d;
}

void main() {
    vec2 uv = fragCoord * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.3;
    float bass = iBandEnergy[0] + iBandEnergy[1];

    // Camera orbit
    float camAngle = t;
    vec3 ro = vec3(sin(camAngle) * 5.0, 1.5 + bass * 0.5, cos(camAngle) * 5.0);
    vec3 target = vec3(0.0, 0.0, 0.0);
    vec3 fwd = normalize(target - ro);
    vec3 right = normalize(cross(vec3(0, 1, 0), fwd));
    vec3 up = cross(fwd, right);
    vec3 rd = normalize(fwd + uv.x * right + uv.y * up);

    // March
    float d = march(ro, rd);

    // Background — gradient
    vec3 col = mix(vec3(0.01, 0.01, 0.03), vec3(0.05, 0.0, 0.1), fragCoord.y);

    if (d < MAX_DIST) {
        vec3 p = ro + rd * d;
        vec3 n = getNormal(p);

        // Determine if floor or object
        bool isFloor = p.y < -1.9;

        // Lighting
        vec3 lightPos = vec3(2.0, 4.0, -3.0);
        vec3 lightDir = normalize(lightPos - p);
        float diff = max(dot(n, lightDir), 0.0);
        float spec = pow(max(dot(reflect(-lightDir, n), -rd), 0.0), 32.0);

        if (isFloor) {
            // Reflective floor with grid pattern
            vec2 grid = abs(fract(p.xz) - 0.5);
            float gridLine = 1.0 - smoothstep(0.02, 0.03, min(grid.x, grid.y));
            col = vec3(0.02) + vec3(0.1, 0.05, 0.15) * gridLine;
            col += vec3(0.1, 0.1, 0.2) * diff;
        } else {
            // Metallic orb surface — iridescent
            float fresnel = pow(1.0 - max(dot(n, -rd), 0.0), 3.0);
            vec3 baseColor = vec3(
                0.3 + 0.3 * sin(t + p.x * 2.0),
                0.2 + 0.3 * sin(t + p.y * 2.0 + 2.0),
                0.5 + 0.3 * sin(t + p.z * 2.0 + 4.0)
            );
            col = baseColor * diff * 0.6;
            col += vec3(0.8, 0.7, 1.0) * spec * (0.5 + iBeat);
            col += vec3(0.3, 0.4, 0.9) * fresnel * (0.5 + bass * 0.5);
        }

        // Fog
        float fog = 1.0 - exp(-d * 0.08);
        col = mix(col, vec3(0.02, 0.01, 0.05), fog);
    }

    // Beat: add subtle bloom
    col += vec3(0.05, 0.02, 0.08) * iBeat;

    // Gamma
    col = pow(col, vec3(0.9));

    fragColor = vec4(col, 1.0);
}
