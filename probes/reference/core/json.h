// SPDX-License-Identifier: MIT
// Live Probe core: a very small JSON reader (requests) and writer (responses, events).
#pragma once

#include <cmath>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace liveprobe {

struct JValue {
    enum Type { Null, Bool, Number, String, Array, Object } type = Null;
    bool b = false;
    double num = 0;
    std::string str;
    std::vector<JValue> arr;
    std::map<std::string, JValue> obj;

    const JValue* get(const std::string& key) const {
        if (type != Object) return nullptr;
        auto it = obj.find(key);
        return it == obj.end() ? nullptr : &it->second;
    }
    bool isNumber() const { return type == Number; }
    bool isString() const { return type == String; }
};

/// Parses one JSON document. Returns false (and sets err) on malformed input.
bool parseJson(const std::string& text, JValue& out, std::string& err);

/// Streaming writer. Keys and commas are handled by the caller through key()/value helpers.
class JWriter {
public:
    JWriter& beginObject() { sep(); s_ += '{'; first_.push_back(true); return *this; }
    JWriter& endObject() { s_ += '}'; first_.pop_back(); return *this; }
    JWriter& beginArray() { sep(); s_ += '['; first_.push_back(true); return *this; }
    JWriter& endArray() { s_ += ']'; first_.pop_back(); return *this; }
    JWriter& key(const char* k) { sep(); quote(k); s_ += ':'; afterKey_ = true; return *this; }
    JWriter& str(const std::string& v) { sep(); quote(v); return *this; }
    JWriter& boolean(bool v) { sep(); s_ += v ? "true" : "false"; return *this; }
    JWriter& null() { sep(); s_ += "null"; return *this; }
    JWriter& integer(long long v) { sep(); s_ += std::to_string(v); return *this; }
    /// Fixed decimals; NaN/inf become null.
    JWriter& number(double v, int decimals = 3);
    /// dB value with 1 decimal; anything below -150 (or -inf/NaN) is -150.0.
    JWriter& db(double v) { return number(clampDb(v), 1); }
    JWriter& raw(const std::string& json) { sep(); s_ += json; return *this; }

    static double clampDb(double v) { return (std::isnan(v) || v < -150.0) ? -150.0 : v; }
    const std::string& text() const { return s_; }
    std::string take() { return std::move(s_); }

private:
    void sep() {
        if (afterKey_) { afterKey_ = false; return; }
        if (!first_.empty()) {
            if (!first_.back()) s_ += ',';
            first_.back() = false;
        }
    }
    void quote(const std::string& v);
    std::string s_;
    std::vector<bool> first_;
    bool afterKey_ = false;
};

}  // namespace liveprobe
