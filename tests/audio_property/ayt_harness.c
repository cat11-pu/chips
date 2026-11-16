/*
    AYT property-test harness for chips/ay38910.h.

    AYT is a small, deliberately strict register-frame container used by the
    audio property tests. It is not an emulator ROM format. One AYT frame is
    one video frame of AY register writes; frame references allow a write
    block to be shared by several frames.
*/
#define CHIPS_IMPL
#include "chips/ay38910.h"

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define AYT_HEADER_SIZE          (40u)
#define AYT_FRAME_ENTRY_SIZE     (8u)
#define AYT_OP_SIZE              (8u)
#define AYT_MAX_FILE_SIZE        (4u * 1024u * 1024u)
#define AYT_MAX_FRAMES           (4096u)
#define AYT_MAX_OPS_PER_FRAME    (256u)
#define AYT_MAX_REF_DEPTH        (64u)
#define AYT_SAMPLE_RATE          (44100)

#define AYT_MAGIC                (0x31545941u) /* "AYT1" little endian */
#define AYT_VERSION              (1u)
#define AYT_NO_LOOP              (0xFFFFFFFFu)

typedef enum {
    AYT_OK = 0,
    AYT_TRUNCATED,
    AYT_OVERSIZED,
    AYT_INVALID_REGISTER,
    AYT_CYCLE,
    AYT_BAD_FORMAT,
    AYT_IO
} ayt_error_kind_t;

typedef struct {
    uint8_t kind;       /* 0 = register write, 1 = frame reference */
    uint8_t reg;
    uint16_t value;
    uint32_t target;
    uint32_t byte_offset;
} ayt_op_t;

typedef struct {
    uint32_t offset;
    uint16_t op_count;
    ayt_op_t* ops;
} ayt_frame_t;

typedef struct {
    ayt_error_kind_t kind;
    uint32_t frame;
    uint64_t byte_offset;
    char message[160];
} ayt_error_t;

typedef struct {
    uint8_t* data;
    size_t size;
    uint32_t clock_hz;
    uint16_t frame_rate;
    uint32_t frame_count;
    uint32_t play_frames;
    uint32_t loop_frame;
    ayt_frame_t* frames;
    ayt_error_t error;
} ayt_file_t;

static const char* ayt_error_name(ayt_error_kind_t kind) {
    switch (kind) {
        case AYT_OK:                return "ok";
        case AYT_TRUNCATED:         return "truncated";
        case AYT_OVERSIZED:         return "oversized";
        case AYT_INVALID_REGISTER:  return "invalid_register";
        case AYT_CYCLE:             return "cycle";
        case AYT_BAD_FORMAT:        return "bad_format";
        case AYT_IO:                return "io";
        default:                    return "unknown";
    }
}

static void ayt_set_error(ayt_file_t* file, ayt_error_kind_t kind,
                          uint32_t frame, uint64_t byte_offset,
                          const char* fmt, ...) {
    if (file->error.kind != AYT_OK) {
        return;
    }
    file->error.kind = kind;
    file->error.frame = frame;
    file->error.byte_offset = byte_offset;
    va_list args;
    va_start(args, fmt);
    vsnprintf(file->error.message, sizeof(file->error.message), fmt, args);
    va_end(args);
}

static uint16_t ayt_read_u16(const uint8_t* p) {
    uint16_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static uint32_t ayt_read_u32(const uint8_t* p) {
    uint32_t value;
    memcpy(&value, p, sizeof(value));
    return value;
}

static int ayt_size_add(size_t a, size_t b, size_t* out) {
    if (a > SIZE_MAX - b) {
        return 0;
    }
    *out = a + b;
    return 1;
}

static int ayt_size_mul(size_t a, size_t b, size_t* out) {
    if ((a != 0) && (b > SIZE_MAX / a)) {
        return 0;
    }
    *out = a * b;
    return 1;
}

static int ayt_load_file(const char* path, ayt_file_t* file) {
    memset(file, 0, sizeof(*file));
    FILE* fp = fopen(path, "rb");
    if (!fp) {
        ayt_set_error(file, AYT_IO, 0, 0, "cannot open %s: %s", path, strerror(errno));
        return 0;
    }
    if (fseek(fp, 0, SEEK_END) != 0) {
        ayt_set_error(file, AYT_IO, 0, 0, "seek failed: %s", strerror(errno));
        fclose(fp);
        return 0;
    }
    long len = ftell(fp);
    if (len < 0) {
        ayt_set_error(file, AYT_IO, 0, 0, "tell failed: %s", strerror(errno));
        fclose(fp);
        return 0;
    }
    if ((unsigned long)len > AYT_MAX_FILE_SIZE) {
        ayt_set_error(file, AYT_OVERSIZED, 0, 0,
                      "file is %ld bytes, limit is %u", len, AYT_MAX_FILE_SIZE);
        fclose(fp);
        return 0;
    }
    rewind(fp);
    file->size = (size_t)len;
    file->data = (uint8_t*)malloc(file->size ? file->size : 1u);
    if (!file->data) {
        ayt_set_error(file, AYT_IO, 0, 0, "out of memory");
        fclose(fp);
        return 0;
    }
    if ((file->size != 0) && (fread(file->data, 1, file->size, fp) != file->size)) {
        ayt_set_error(file, AYT_IO, 0, 0, "short read");
        fclose(fp);
        return 0;
    }
    fclose(fp);
    return 1;
}

static void ayt_free(ayt_file_t* file) {
    if (file->frames) {
        for (uint32_t i = 0; i < file->frame_count; i++) {
            free(file->frames[i].ops);
        }
        free(file->frames);
    }
    free(file->data);
    memset(file, 0, sizeof(*file));
}

static int ayt_validate_graph(ayt_file_t* file) {
    uint8_t* color = (uint8_t*)calloc(file->frame_count, 1);
    uint32_t* frame_stack = (uint32_t*)malloc((size_t)file->frame_count * sizeof(uint32_t));
    uint32_t* op_stack = (uint32_t*)calloc(file->frame_count, sizeof(uint32_t));
    if (!color || !frame_stack || !op_stack) {
        free(color);
        free(frame_stack);
        free(op_stack);
        ayt_set_error(file, AYT_IO, 0, 0, "out of memory");
        return 0;
    }

    for (uint32_t root = 0; root < file->frame_count; root++) {
        if (color[root] != 0) {
            continue;
        }
        uint32_t depth = 0;
        frame_stack[depth] = root;
        op_stack[depth] = 0;
        color[root] = 1;
        depth++;
        while (depth > 0) {
            uint32_t frame_index = frame_stack[depth - 1];
            uint32_t op_index = op_stack[depth - 1];
            const ayt_frame_t* frame = &file->frames[frame_index];
            if (op_index >= frame->op_count) {
                color[frame_index] = 2;
                depth--;
                continue;
            }
            op_stack[depth - 1] = op_index + 1;
            const ayt_op_t* op = &frame->ops[op_index];
            if (op->kind == 1) {
                if (op->target >= file->frame_count) {
                    ayt_set_error(file, AYT_BAD_FORMAT, frame_index, op->byte_offset,
                                  "reference target %" PRIu32 " out of range", op->target);
                    free(color);
                    free(frame_stack);
                    free(op_stack);
                    return 0;
                }
                if (color[op->target] == 1) {
                    ayt_set_error(file, AYT_CYCLE, frame_index, op->byte_offset,
                                  "frame %" PRIu32 " references frame %" PRIu32
                                  " while it is active", frame_index, op->target);
                    free(color);
                    free(frame_stack);
                    free(op_stack);
                    return 0;
                }
                if (color[op->target] == 0) {
                    if (depth >= AYT_MAX_REF_DEPTH) {
                        ayt_set_error(file, AYT_CYCLE, frame_index, op->byte_offset,
                                      "reference depth exceeds %u", AYT_MAX_REF_DEPTH);
                        free(color);
                        free(frame_stack);
                        free(op_stack);
                        return 0;
                    }
                    color[op->target] = 1;
                    frame_stack[depth] = op->target;
                    op_stack[depth] = 0;
                    depth++;
                }
            }
        }
    }
    free(color);
    free(frame_stack);
    free(op_stack);
    return 1;
}

static int ayt_parse(ayt_file_t* file) {
    if (file->error.kind != AYT_OK) {
        return 0;
    }
    if (file->size < AYT_HEADER_SIZE) {
        ayt_set_error(file, AYT_TRUNCATED, 0, file->size,
                      "truncated header: got %zu bytes, need %u",
                      file->size, AYT_HEADER_SIZE);
        return 0;
    }
    if (ayt_read_u32(file->data) != AYT_MAGIC) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 0, "bad magic");
        return 0;
    }
    uint16_t version = ayt_read_u16(file->data + 4);
    uint16_t header_size = ayt_read_u16(file->data + 6);
    file->clock_hz = ayt_read_u32(file->data + 8);
    file->frame_rate = ayt_read_u16(file->data + 12);
    uint16_t flags = ayt_read_u16(file->data + 14);
    file->frame_count = ayt_read_u32(file->data + 16);
    file->play_frames = ayt_read_u32(file->data + 20);
    file->loop_frame = ayt_read_u32(file->data + 24);
    uint32_t directory_offset = ayt_read_u32(file->data + 28);
    uint32_t payload_bytes = ayt_read_u32(file->data + 32);
    uint32_t reserved = ayt_read_u32(file->data + 36);

    if ((version != AYT_VERSION) || (header_size != AYT_HEADER_SIZE) ||
        (flags != 0) || (reserved != 0)) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 0,
                      "unsupported header version=%u size=%u flags=0x%x reserved=0x%x",
                      version, header_size, flags, reserved);
        return 0;
    }
    if ((file->clock_hz < 100000u) || (file->clock_hz > 4000000u)) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 8,
                      "clock_hz %" PRIu32 " outside [100000,4000000]", file->clock_hz);
        return 0;
    }
    if ((file->frame_rate == 0) || (file->frame_rate > 1000)) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 12,
                      "frame_rate %u outside [1,1000]", file->frame_rate);
        return 0;
    }
    if ((file->frame_count == 0) || (file->frame_count > AYT_MAX_FRAMES)) {
        ayt_set_error(file, AYT_OVERSIZED, 0, 16,
                      "frame_count %" PRIu32 " outside [1,%u]",
                      file->frame_count, AYT_MAX_FRAMES);
        return 0;
    }
    if ((file->play_frames == 0) || (file->play_frames > AYT_MAX_FRAMES) ||
        (file->play_frames > file->frame_count)) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 20,
                      "play_frames %" PRIu32 " outside [1,%" PRIu32 "]",
                      file->play_frames, file->frame_count);
        return 0;
    }
    if ((file->loop_frame != AYT_NO_LOOP) && (file->loop_frame >= file->frame_count)) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 24,
                      "loop_frame %" PRIu32 " out of range", file->loop_frame);
        return 0;
    }
    if (directory_offset != AYT_HEADER_SIZE) {
        ayt_set_error(file, AYT_BAD_FORMAT, 0, 28,
                      "directory_offset must be %u", AYT_HEADER_SIZE);
        return 0;
    }
    if (payload_bytes > AYT_MAX_FILE_SIZE) {
        ayt_set_error(file, AYT_OVERSIZED, 0, 32,
                      "payload_bytes %" PRIu32 " exceeds limit", payload_bytes);
        return 0;
    }

    size_t directory_bytes = 0;
    size_t directory_end = 0;
    if (!ayt_size_mul(file->frame_count, AYT_FRAME_ENTRY_SIZE, &directory_bytes) ||
        !ayt_size_add(directory_offset, directory_bytes, &directory_end)) {
        ayt_set_error(file, AYT_OVERSIZED, 0, 28, "directory size overflow");
        return 0;
    }
    if (directory_end > file->size) {
        uint32_t frame = 0;
        if (file->size > directory_offset) {
            frame = (uint32_t)((file->size - directory_offset) / AYT_FRAME_ENTRY_SIZE);
            if (frame >= file->frame_count) {
                frame = file->frame_count - 1;
            }
        }
        ayt_set_error(file, AYT_TRUNCATED, frame, file->size,
                      "truncated frame directory");
        return 0;
    }
    size_t payload_end = 0;
    if (!ayt_size_add(directory_end, payload_bytes, &payload_end) ||
        (payload_end > file->size)) {
        uint32_t frame = 0;
        if (file->size > directory_end) {
            frame = (uint32_t)((file->size - directory_end) / AYT_OP_SIZE);
            if (frame >= file->frame_count) {
                frame = file->frame_count - 1;
            }
        }
        ayt_set_error(file, AYT_TRUNCATED, frame, file->size,
                      "truncated payload: declared %" PRIu32 " bytes", payload_bytes);
        return 0;
    }

    file->frames = (ayt_frame_t*)calloc(file->frame_count, sizeof(ayt_frame_t));
    if (!file->frames) {
        ayt_set_error(file, AYT_IO, 0, 0, "out of memory");
        return 0;
    }
    for (uint32_t i = 0; i < file->frame_count; i++) {
        size_t entry_offset = directory_offset + (size_t)i * AYT_FRAME_ENTRY_SIZE;
        const uint8_t* entry = file->data + entry_offset;
        ayt_frame_t* frame = &file->frames[i];
        frame->offset = ayt_read_u32(entry);
        frame->op_count = ayt_read_u16(entry + 4);
        uint16_t frame_reserved = ayt_read_u16(entry + 6);

        if (frame->op_count > AYT_MAX_OPS_PER_FRAME) {
            ayt_set_error(file, AYT_OVERSIZED, i, entry_offset + 4,
                          "frame %" PRIu32 " declares %u ops, limit is %u",
                          i, frame->op_count, AYT_MAX_OPS_PER_FRAME);
            return 0;
        }
        if (frame_reserved != 0) {
            ayt_set_error(file, AYT_BAD_FORMAT, i, entry_offset + 6,
                          "frame %" PRIu32 " has nonzero reserved field", i);
            return 0;
        }
        size_t op_bytes = 0;
        size_t frame_end = 0;
        if (!ayt_size_mul(frame->op_count, AYT_OP_SIZE, &op_bytes) ||
            !ayt_size_add(frame->offset, op_bytes, &frame_end)) {
            ayt_set_error(file, AYT_OVERSIZED, i, entry_offset,
                          "frame %" PRIu32 " extent overflow", i);
            return 0;
        }
        if ((frame->offset < directory_end) ||
            (frame_end > payload_end) || (frame_end > file->size)) {
            ayt_set_error(file, AYT_TRUNCATED, i, entry_offset,
                          "frame %" PRIu32 " extent [%zu,%zu) outside payload [%zu,%zu)",
                          i, (size_t)frame->offset, frame_end, directory_end, payload_end);
            return 0;
        }
        if (frame->op_count != 0) {
            frame->ops = (ayt_op_t*)calloc(frame->op_count, sizeof(ayt_op_t));
            if (!frame->ops) {
                ayt_set_error(file, AYT_IO, i, entry_offset, "out of memory");
                return 0;
            }
        }
        for (uint32_t j = 0; j < frame->op_count; j++) {
            size_t op_offset = (size_t)frame->offset + (size_t)j * AYT_OP_SIZE;
            const uint8_t* raw = file->data + op_offset;
            ayt_op_t* op = &frame->ops[j];
            op->kind = raw[0];
            op->reg = raw[1];
            op->value = ayt_read_u16(raw + 2);
            op->target = ayt_read_u32(raw + 4);
            op->byte_offset = (uint32_t)op_offset;
            if (op->kind == 0) {
                if (op->reg >= AY38910_NUM_REGISTERS) {
                    ayt_set_error(file, AYT_INVALID_REGISTER, i, op_offset + 1,
                                  "frame %" PRIu32 " writes AY register %u",
                                  i, op->reg);
                    return 0;
                }
                if (op->target != 0) {
                    ayt_set_error(file, AYT_BAD_FORMAT, i, op_offset + 4,
                                  "write op has nonzero target");
                    return 0;
                }
            }
            else if (op->kind == 1) {
                if ((op->reg != 0) || (op->value != 0)) {
                    ayt_set_error(file, AYT_BAD_FORMAT, i, op_offset + 1,
                                  "reference op has nonzero reserved fields");
                    return 0;
                }
            }
            else {
                ayt_set_error(file, AYT_BAD_FORMAT, i, op_offset,
                              "unknown op kind %u", op->kind);
                return 0;
            }
        }
    }
    if (!ayt_validate_graph(file)) {
        return 0;
    }
    return 1;
}

static int ayt_open(const char* path, ayt_file_t* file) {
    if (!ayt_load_file(path, file)) {
        return 0;
    }
    return ayt_parse(file);
}

static void ayt_write_reg(ay38910_t* ay, uint8_t reg, uint8_t value) {
    uint64_t pins = AY38910_BDIR | AY38910_BC1;
    AY38910_SET_DATA(pins, reg);
    (void)ay38910_iorq(ay, pins);
    pins = AY38910_BDIR;
    AY38910_SET_DATA(pins, value);
    (void)ay38910_iorq(ay, pins);
}

static void ayt_apply_frame(ay38910_t* ay, const ayt_file_t* file,
                            uint32_t frame_index, uint32_t depth) {
    if (depth > AYT_MAX_REF_DEPTH) {
        abort();
    }
    const ayt_frame_t* frame = &file->frames[frame_index];
    for (uint32_t i = 0; i < frame->op_count; i++) {
        const ayt_op_t* op = &frame->ops[i];
        if (op->kind == 0) {
            ayt_write_reg(ay, op->reg, (uint8_t)op->value);
        }
        else {
            ayt_apply_frame(ay, file, op->target, depth + 1);
        }
    }
}

static int ayt_render(const char* input_path, const char* output_path) {
    ayt_file_t file;
    if (!ayt_open(input_path, &file)) {
        fprintf(stderr, "parse failed: %s at frame %" PRIu32
                " byte %" PRIu64 ": %s\n",
                ayt_error_name(file.error.kind), file.error.frame,
                file.error.byte_offset, file.error.message);
        ayt_free(&file);
        return 2;
    }
    uint32_t samples_per_frame = (uint32_t)AYT_SAMPLE_RATE / file.frame_rate;
    if (samples_per_frame == 0) {
        ayt_free(&file);
        return 2;
    }
    uint64_t total_samples = (uint64_t)file.play_frames * samples_per_frame;
    if (total_samples > UINT32_MAX) {
        ayt_free(&file);
        return 2;
    }

    ay38910_t ay;
    ay38910_init(&ay, &(ay38910_desc_t){
        .type = AY38910_TYPE_8910,
        .tick_hz = (int)file.clock_hz,
        .sound_hz = AYT_SAMPLE_RATE,
        .magnitude = 0.5f,
    });
    ay38910_reset(&ay);

    FILE* fp = fopen(output_path, "wb");
    if (!fp) {
        ayt_free(&file);
        return 2;
    }
    uint32_t sample_count = (uint32_t)total_samples;
    if (fwrite(&sample_count, sizeof(sample_count), 1, fp) != 1) {
        fclose(fp);
        ayt_free(&file);
        return 2;
    }
    for (uint32_t frame_index = 0; frame_index < file.play_frames; frame_index++) {
        ayt_apply_frame(&ay, &file, frame_index, 0);
        for (uint32_t s = 0; s < samples_per_frame; s++) {
            while (!ay38910_tick(&ay)) {
            }
            float sample = ay.sample;
            if (fwrite(&sample, sizeof(sample), 1, fp) != 1) {
                fclose(fp);
                ayt_free(&file);
                return 2;
            }
        }
    }
    fclose(fp);
    ayt_free(&file);
    return 0;
}

static int ayt_check(const char* input_path, const char* expected_kind,
                     uint32_t expected_frame) {
    ayt_file_t file;
    int ok = ayt_open(input_path, &file);
    if (ok) {
        printf("{\"status\":\"accepted\",\"kind\":\"none\",\"frame\":null,"
               "\"byte\":null,\"expected_kind\":\"%s\",\"expected_frame\":%"
               PRIu32 "}\n", expected_kind, expected_frame);
        ayt_free(&file);
        return 2;
    }
    const char* kind = ayt_error_name(file.error.kind);
    printf("{\"status\":\"reject\",\"kind\":\"%s\",\"frame\":%" PRIu32
           ",\"byte\":%" PRIu64 ",\"expected_kind\":\"%s\","
           "\"expected_frame\":%" PRIu32 ",\"message\":\"%s\"}\n",
           kind, file.error.frame, file.error.byte_offset, expected_kind,
           expected_frame, file.error.message);
    int match = (strcmp(kind, expected_kind) == 0) &&
                (file.error.frame == expected_frame);
    ayt_free(&file);
    return match ? 0 : 3;
}

static uint32_t ayt_xorshift32(uint32_t* state) {
    uint32_t x = *state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

static int ayt_stress(uint32_t frames, uint32_t seed) {
    ay38910_t ay;
    ay38910_init(&ay, &(ay38910_desc_t){
        .type = AY38910_TYPE_8910,
        .tick_hz = 1500000,
        .sound_hz = AYT_SAMPLE_RATE,
        .magnitude = 0.5f,
    });
    uint32_t state = seed ? seed : 1u;
    uint64_t writes = 0;
    uint64_t invalid_addr_writes = 0;
    uint64_t ticks = 0;
    for (uint32_t frame = 0; frame < frames; frame++) {
        for (uint32_t i = 0; i < 16; i++) {
            uint8_t reg = (uint8_t)ayt_xorshift32(&state);
            uint8_t value = (uint8_t)ayt_xorshift32(&state);
            ayt_write_reg(&ay, reg, value);
            writes++;
            if (reg >= AY38910_NUM_REGISTERS) {
                invalid_addr_writes++;
            }
        }
        for (uint32_t i = 0; i < 64; i++) {
            (void)ay38910_tick(&ay);
            ticks++;
        }
        if (!isfinite(ay.sample)) {
            printf("{\"status\":\"fail\",\"reason\":\"nonfinite_sample\","
                   "\"frame\":%" PRIu32 "}\n", frame);
            return 4;
        }
    }
    printf("{\"status\":\"pass\",\"frames\":%" PRIu32 ",\"writes\":%" PRIu64
           ",\"invalid_addr_writes\":%" PRIu64 ",\"ticks\":%" PRIu64 "}\n",
           frames, writes, invalid_addr_writes, ticks);
    return 0;
}

static void ayt_usage(const char* argv0) {
    fprintf(stderr,
            "usage:\n"
            "  %s check-corrupt <file.ayt> <kind> <frame>\n"
            "  %s render <file.ayt> <out.pcmf32>\n"
            "  %s stress <frames> <seed>\n",
            argv0, argv0, argv0);
}

int main(int argc, char** argv) {
    if (argc == 5 && strcmp(argv[1], "check-corrupt") == 0) {
        char* end = NULL;
        unsigned long frame = strtoul(argv[4], &end, 10);
        if (!end || *end || frame > UINT32_MAX) {
            return 2;
        }
        return ayt_check(argv[2], argv[3], (uint32_t)frame);
    }
    if (argc == 4 && strcmp(argv[1], "render") == 0) {
        return ayt_render(argv[2], argv[3]);
    }
    if (argc == 4 && strcmp(argv[1], "stress") == 0) {
        char* end = NULL;
        unsigned long frames = strtoul(argv[2], &end, 10);
        if (!end || *end || frames > 1000000ul) {
            return 2;
        }
        end = NULL;
        unsigned long seed = strtoul(argv[3], &end, 10);
        if (!end || *end || seed > UINT32_MAX) {
            return 2;
        }
        return ayt_stress((uint32_t)frames, (uint32_t)seed);
    }
    ayt_usage(argv[0]);
    return 2;
}
