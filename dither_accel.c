
#include <stdint.h>

void atkinson_1bit(int16_t* pixels, int w, int h) {
    for (int y = 0; y < h; y++) {
        int row_off = y * w;
        int next_off = row_off + w;
        int next2_off = row_off + (w * 2);
        int has_next_y = (y + 1 < h);
        int has_next2_y = (y + 2 < h);
        for (int x = 0; x < w; x++) {
            int idx = row_off + x;
            int16_t old_val = pixels[idx];
            int16_t new_val = (old_val > 127) ? 255 : 0;
            pixels[idx] = new_val;
            int16_t err = (old_val - new_val) >> 3;
            if (err) {
                if (x + 1 < w) pixels[idx + 1] += err;
                if (x + 2 < w) pixels[idx + 2] += err;
                if (has_next_y) {
                    if (x > 0) pixels[next_off + x - 1] += err;
                    pixels[next_off + x] += err;
                    if (x + 1 < w) pixels[next_off + x + 1] += err;
                }
                if (has_next2_y) {
                    pixels[next2_off + x] += err;
                }
            }
        }
    }
}

void atkinson_2bit(int16_t* pixels, int w, int h) {
    for (int y = 0; y < h; y++) {
        int row_off = y * w;
        int next_off = row_off + w;
        int next2_off = row_off + (w * 2);
        int has_next_y = (y + 1 < h);
        int has_next2_y = (y + 2 < h);
        for (int x = 0; x < w; x++) {
            int idx = row_off + x;
            int16_t old_val = pixels[idx];
            int v = (old_val + 42) / 85;
            if (v < 0) v = 0;
            if (v > 3) v = 3;
            int16_t new_val = (int16_t)(v * 85);
            pixels[idx] = new_val;
            int16_t err = (old_val - new_val) >> 3;
            if (err) {
                if (x + 1 < w) pixels[idx + 1] += err;
                if (x + 2 < w) pixels[idx + 2] += err;
                if (has_next_y) {
                    if (x > 0) pixels[next_off + x - 1] += err;
                    pixels[next_off + x] += err;
                    if (x + 1 < w) pixels[next_off + x + 1] += err;
                }
                if (has_next2_y) {
                    pixels[next2_off + x] += err;
                }
            }
        }
    }
}
