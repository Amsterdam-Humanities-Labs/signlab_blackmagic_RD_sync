// Print clip metadata (dimensions, framerate, frame count, timecode) for a
// .braw file using the Blackmagic RAW SDK. Timecode is read from frame 0
// and from the last frame; if the clip has IBlackmagicRawClipEx, drop-frame
// info is included.
//
// Usage: braw_probe <input.braw>

#include "BlackmagicRawAPI.h"

#include <cstdint>
#include <cstdio>
#include <string>

#include <CoreFoundation/CoreFoundation.h>

static std::string cfToStdString(CFStringRef s) {
    if (s == nullptr) return {};
    CFIndex len = CFStringGetLength(s);
    CFIndex maxBytes = CFStringGetMaximumSizeForEncoding(len, kCFStringEncodingUTF8) + 1;
    std::string out(maxBytes, '\0');
    if (!CFStringGetCString(s, out.data(), maxBytes, kCFStringEncodingUTF8)) {
        return {};
    }
    out.resize(std::char_traits<char>::length(out.c_str()));
    return out;
}

int main(int argc, const char* argv[]) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <input.braw>\n", argv[0]);
        return 2;
    }
    const char* inputPath = argv[1];

    CFStringRef cfInput = CFStringCreateWithCString(
        nullptr, inputPath, kCFStringEncodingUTF8);
    CFStringRef cfFrameworkPath = CFSTR(
        "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries");

    IBlackmagicRawFactory* factory =
        CreateBlackmagicRawFactoryInstanceFromPath(cfFrameworkPath);
    if (!factory) {
        std::fprintf(stderr, "failed to load BRAW SDK framework\n");
        CFRelease(cfInput);
        return 3;
    }
    IBlackmagicRaw* codec = nullptr;
    if (factory->CreateCodec(&codec) != S_OK) {
        std::fprintf(stderr, "CreateCodec failed\n");
        factory->Release(); CFRelease(cfInput);
        return 4;
    }
    IBlackmagicRawClip* clip = nullptr;
    HRESULT hr = codec->OpenClip(cfInput, &clip);
    if (hr != S_OK) {
        std::fprintf(stderr, "OpenClip failed (hr=0x%x)\n", hr);
        codec->Release(); factory->Release(); CFRelease(cfInput);
        return 5;
    }

    uint32_t width = 0, height = 0;
    float fps = 0.0f;
    uint64_t frameCount = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);
    clip->GetFrameRate(&fps);
    clip->GetFrameCount(&frameCount);

    std::printf("file:        %s\n", inputPath);
    std::printf("dimensions:  %ux%u\n", width, height);
    std::printf("framerate:   %.3f fps\n", fps);
    std::printf("frame count: %llu\n", (unsigned long long)frameCount);
    std::printf("duration:    %.3f s\n",
                fps > 0 ? (double)frameCount / fps : 0.0);

    CFStringRef tcFirst = nullptr;
    if (clip->GetTimecodeForFrame(0, &tcFirst) == S_OK && tcFirst) {
        std::printf("timecode[0]: %s\n", cfToStdString(tcFirst).c_str());
        CFRelease(tcFirst);
    } else {
        std::printf("timecode[0]: <unavailable>\n");
    }

    if (frameCount > 0) {
        CFStringRef tcLast = nullptr;
        uint64_t lastIdx = frameCount - 1;
        if (clip->GetTimecodeForFrame(lastIdx, &tcLast) == S_OK && tcLast) {
            std::printf("timecode[%llu]: %s\n",
                        (unsigned long long)lastIdx,
                        cfToStdString(tcLast).c_str());
            CFRelease(tcLast);
        } else {
            std::printf("timecode[%llu]: <unavailable>\n",
                        (unsigned long long)lastIdx);
        }
    }

    IBlackmagicRawClipEx* clipEx = nullptr;
    if (clip->QueryInterface(IID_IBlackmagicRawClipEx, (void**)&clipEx) == S_OK
        && clipEx != nullptr) {
        uint32_t baseIdx = 0;
        bool isDropFrame = false;
        if (clipEx->QueryTimecodeInfo(&baseIdx, &isDropFrame) == S_OK) {
            std::printf("base frame index: %u\n", baseIdx);
            std::printf("drop frame:       %s\n",
                        isDropFrame ? "yes" : "no");
        }
        clipEx->Release();
    }

    clip->Release();
    codec->Release();
    factory->Release();
    CFRelease(cfInput);
    return 0;
}
