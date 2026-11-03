// SPDX-License-Identifier: MIT
#include "json.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace liveprobe {

namespace {

struct Parser {
    const std::string& s;
    size_t i = 0;
    std::string err;
    int depth = 0;

    void ws() {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) i++;
    }
    bool fail(const char* m) {
        if (err.empty()) err = std::string(m) + " at offset " + std::to_string(i);
        return false;
    }
    bool lit(const char* w) {
        size_t n = strlen(w);
        if (s.compare(i, n, w) != 0) return fail("bad literal");
        i += n;
        return true;
    }
    static void utf8(std::string& o, unsigned cp) {
        if (cp < 0x80) o += (char)cp;
        else if (cp < 0x800) { o += (char)(0xC0 | (cp >> 6)); o += (char)(0x80 | (cp & 0x3F)); }
        else if (cp < 0x10000) {
            o += (char)(0xE0 | (cp >> 12)); o += (char)(0x80 | ((cp >> 6) & 0x3F)); o += (char)(0x80 | (cp & 0x3F));
        } else {
            o += (char)(0xF0 | (cp >> 18)); o += (char)(0x80 | ((cp >> 12) & 0x3F));
            o += (char)(0x80 | ((cp >> 6) & 0x3F)); o += (char)(0x80 | (cp & 0x3F));
        }
    }
    bool hex4(unsigned& v) {
        if (i + 4 > s.size()) return fail("bad escape");
        v = 0;
        for (int k = 0; k < 4; k++) {
            char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else return fail("bad hex escape");
        }
        return true;
    }
    bool string(std::string& o) {
        if (s[i] != '"') return fail("expected string");
        i++;
        while (i < s.size()) {
            char c = s[i++];
            if (c == '"') return true;
            if (c == '\\') {
                if (i >= s.size()) break;
                char e = s[i++];
                switch (e) {
                    case '"': o += '"'; break;
                    case '\\': o += '\\'; break;
                    case '/': o += '/'; break;
                    case 'b': o += '\b'; break;
                    case 'f': o += '\f'; break;
                    case 'n': o += '\n'; break;
                    case 'r': o += '\r'; break;
                    case 't': o += '\t'; break;
                    case 'u': {
                        unsigned cp;
                        if (!hex4(cp)) return false;
                        if (cp >= 0xD800 && cp < 0xDC00 && i + 6 <= s.size() && s[i] == '\\' && s[i + 1] == 'u') {
                            i += 2;
                            unsigned lo;
                            if (!hex4(lo)) return false;
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        }
                        utf8(o, cp);
                        break;
                    }
                    default: return fail("bad escape");
                }
            } else {
                o += c;
            }
        }
        return fail("unterminated string");
    }
    bool value(JValue& v) {
        if (++depth > 32) return fail("too deep");
        ws();
        if (i >= s.size()) return fail("unexpected end");
        char c = s[i];
        bool ok = true;
        if (c == '{') {
            v.type = JValue::Object;
            i++;
            ws();
            if (i < s.size() && s[i] == '}') { i++; }
            else {
                while (true) {
                    ws();
                    if (i >= s.size()) { ok = fail("unexpected end"); break; }
                    std::string k;
                    if (!string(k)) { ok = false; break; }
                    ws();
                    if (i >= s.size() || s[i] != ':') { ok = fail("expected ':'"); break; }
                    i++;
                    JValue item;
                    if (!value(item)) { ok = false; break; }
                    v.obj[k] = std::move(item);
                    ws();
                    if (i < s.size() && s[i] == ',') { i++; continue; }
                    if (i < s.size() && s[i] == '}') { i++; break; }
                    ok = fail("expected ',' or '}'");
                    break;
                }
            }
        } else if (c == '[') {
            v.type = JValue::Array;
            i++;
            ws();
            if (i < s.size() && s[i] == ']') { i++; }
            else {
                while (true) {
                    JValue item;
                    if (!value(item)) { ok = false; break; }
                    v.arr.push_back(std::move(item));
                    ws();
                    if (i < s.size() && s[i] == ',') { i++; continue; }
                    if (i < s.size() && s[i] == ']') { i++; break; }
                    ok = fail("expected ',' or ']'");
                    break;
                }
            }
        } else if (c == '"') {
            v.type = JValue::String;
            ok = string(v.str);
        } else if (c == 't') { v.type = JValue::Bool; v.b = true; ok = lit("true"); }
        else if (c == 'f') { v.type = JValue::Bool; v.b = false; ok = lit("false"); }
        else if (c == 'n') { v.type = JValue::Null; ok = lit("null"); }
        else if (c == '-' || (c >= '0' && c <= '9')) {
            const char* start = s.c_str() + i;
            char* end = nullptr;
            v.type = JValue::Number;
            v.num = strtod(start, &end);
            if (end == start) ok = fail("bad number");
            else i += (size_t)(end - start);
        } else {
            ok = fail("unexpected character");
        }
        depth--;
        return ok;
    }
};

}  // namespace

bool parseJson(const std::string& text, JValue& out, std::string& err) {
    Parser p{text};
    out = JValue();
    if (!p.value(out)) { err = p.err; return false; }
    p.ws();
    if (p.i != text.size()) { err = "trailing characters"; return false; }
    return true;
}

JWriter& JWriter::number(double v, int decimals) {
    if (std::isnan(v) || std::isinf(v)) return null();
    sep();
    char buf[64];
    snprintf(buf, sizeof buf, "%.*f", decimals, v);
    // "-0.0" reads oddly in a meter
    if (buf[0] == '-') {
        bool zero = true;
        for (char* p = buf + 1; *p; p++) if (*p != '0' && *p != '.') { zero = false; break; }
        if (zero) { s_ += buf + 1; return *this; }
    }
    s_ += buf;
    return *this;
}

void JWriter::quote(const std::string& v) {
    s_ += '"';
    for (unsigned char c : v) {
        switch (c) {
            case '"': s_ += "\\\""; break;
            case '\\': s_ += "\\\\"; break;
            case '\n': s_ += "\\n"; break;
            case '\r': s_ += "\\r"; break;
            case '\t': s_ += "\\t"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    snprintf(b, sizeof b, "\\u%04x", c);
                    s_ += b;
                } else {
                    s_ += (char)c;
                }
        }
    }
    s_ += '"';
}

}  // namespace liveprobe
