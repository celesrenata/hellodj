#version 330 core

// Fosfora particle physics — transform feedback vertex shader.
// Simulates gravity, drag, lifetime decay per particle.
// Output captured via transform feedback into the destination VBO.

// Per-particle attributes (source buffer)
layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_velocity;
layout(location = 2) in float in_lifetime;
layout(location = 3) in vec4 in_color;

// Transform feedback outputs (captured into destination buffer)
out vec3 out_position;
out vec3 out_velocity;
out float out_lifetime;
out vec4 out_color;

// Uniforms
uniform float u_dt;           // Delta time (seconds)
uniform float u_gravity;      // Gravity strength (downward)
uniform float u_drag;         // Velocity damping factor per second
uniform float u_beat;         // 0.0-1.0 beat pulse (decaying)
uniform float u_bpm;          // Current BPM for color cycling
uniform float u_time;         // Elapsed time for color cycling
uniform float u_band_energy[7]; // 7-band energy levels

// Emission uniforms
uniform int   u_emit_count;   // Number of new particles to emit this frame
uniform float u_emit_speed;   // Base emission speed
uniform int   u_particle_id;  // gl_VertexID offset for emission seeding

void main() {
    float lifetime = in_lifetime - u_dt;

    if (lifetime <= 0.0) {
        // Dead particle — check if we should re-emit
        int vid = gl_VertexID;
        if (vid < u_emit_count) {
            // Re-emit from center with random-ish velocity
            // Use vertex ID + time for pseudo-random direction
            float angle = float(vid) * 2.39996323 + u_time * 3.14159;
            float z_angle = float(vid) * 1.61803398 + u_time * 2.71828;
            float speed = u_emit_speed * (0.5 + 0.5 * u_beat);

            out_position = vec3(0.0, 0.0, 0.0);
            out_velocity = vec3(
                cos(angle) * cos(z_angle) * speed,
                sin(angle) * cos(z_angle) * speed,
                sin(z_angle) * speed * 0.5
            );
            out_lifetime = 2.0 + sin(float(vid) * 0.7) * 1.0;

            // Color cycling driven by BPM
            float hue = fract(u_time * u_bpm / 120.0 + float(vid) * 0.1);
            // HSV to RGB approximation
            vec3 rgb = clamp(abs(mod(hue * 6.0 + vec3(0.0, 4.0, 2.0), 6.0) - 3.0) - 1.0, 0.0, 1.0);
            out_color = vec4(rgb, 1.0);
        } else {
            // Stay dead
            out_position = vec3(0.0);
            out_velocity = vec3(0.0);
            out_lifetime = 0.0;
            out_color = vec4(0.0);
        }
    } else {
        // Alive — simulate physics
        vec3 vel = in_velocity;

        // Apply gravity (negative Y)
        vel.y -= u_gravity * u_dt;

        // Apply drag
        float drag = pow(1.0 - u_drag, u_dt);
        vel *= drag;

        // Beat impulse — push outward from center on beat
        if (u_beat > 0.5) {
            vec3 dir = normalize(in_position + vec3(0.001));
            vel += dir * u_beat * 0.3 * u_dt;
        }

        // Update position
        vec3 pos = in_position + vel * u_dt;

        // Fade alpha as lifetime decreases
        float alpha = smoothstep(0.0, 0.5, lifetime);

        out_position = pos;
        out_velocity = vel;
        out_lifetime = lifetime;
        out_color = vec4(in_color.rgb, in_color.a * alpha);
    }
}
