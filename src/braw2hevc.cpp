// Transcode a Blackmagic RAW (.braw) clip to H.265 (.mp4) by piping decoded
// RGBA frames into ffmpeg's hevc_videotoolbox encoder.
//
// The BRAW SDK is loaded at runtime from the platform SDK Libraries folder
// via the dispatch shim shipped in the SDK Include/ folder.

#ifdef _WIN32
#include "BlackmagicRawAPIDispatch.h"
#include <comdef.h>
#include <windows.h>
#else
#include "BlackmagicRawAPI.h"
#include <CoreFoundation/CoreFoundation.h>
#endif

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <condition_variable>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

static const BlackmagicRawResourceFormat s_resourceFormat =
    blackmagicRawResourceFormatRGBAU8;

namespace {

struct FrameSlot {
    std::mutex m;
    std::condition_variable cv;
    bool ready = false;
    HRESULT result = S_OK;
    std::vector<uint8_t> data;
};

class SyncCallback : public IBlackmagicRawCallback {
public:
    FrameSlot* slot = nullptr;

    void ReadComplete(IBlackmagicRawJob* readJob, HRESULT result,
                      IBlackmagicRawFrame* frame) override {
        IBlackmagicRawJob* decodeJob = nullptr;
        if (result == S_OK)
            result = frame->SetResourceFormat(s_resourceFormat);
        if (result == S_OK)
            result = frame->CreateJobDecodeAndProcessFrame(nullptr, nullptr,
                                                           &decodeJob);
        if (result == S_OK)
            result = decodeJob->Submit();
        if (result != S_OK) {
            if (decodeJob) decodeJob->Release();
            std::lock_guard<std::mutex> lk(slot->m);
            slot->result = result;
            slot->ready = true;
            slot->cv.notify_one();
        }
        readJob->Release();
    }

    void ProcessComplete(IBlackmagicRawJob* job, HRESULT result,
                         IBlackmagicRawProcessedImage* image) override {
        std::lock_guard<std::mutex> lk(slot->m);
        slot->result = result;
        if (result == S_OK && image != nullptr) {
            uint32_t bytes = 0;
            void* res = nullptr;
            image->GetResourceSizeBytes(&bytes);
            image->GetResource(&res);
            slot->data.assign(static_cast<uint8_t*>(res),
                              static_cast<uint8_t*>(res) + bytes);
        }
        slot->ready = true;
        slot->cv.notify_one();
        job->Release();
    }

    void DecodeComplete(IBlackmagicRawJob*, HRESULT) override {}
    void TrimProgress(IBlackmagicRawJob*, float) override {}
    void TrimComplete(IBlackmagicRawJob*, HRESULT) override {}
#ifdef _WIN32
    void SidecarMetadataParseWarning(IBlackmagicRawClip*, BSTR,
                                     uint32_t, BSTR) override {}
    void SidecarMetadataParseError(IBlackmagicRawClip*, BSTR,
                                   uint32_t, BSTR) override {}
#else
    void SidecarMetadataParseWarning(IBlackmagicRawClip*, CFStringRef,
                                     uint32_t, CFStringRef) override {}
    void SidecarMetadataParseError(IBlackmagicRawClip*, CFStringRef,
                                   uint32_t, CFStringRef) override {}
#endif
    void PreparePipelineComplete(void*, HRESULT) override {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) override {
        return E_NOTIMPL;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return 0; }
    ULONG STDMETHODCALLTYPE Release() override { return 0; }
};

#ifdef _WIN32
std::wstring widenUtf8(const std::string& in) {
    UINT codePage = CP_UTF8;
    int len = MultiByteToWideChar(CP_UTF8, 0, in.c_str(), -1, nullptr, 0);
    if (len <= 0) {
        codePage = CP_ACP;
        len = MultiByteToWideChar(CP_ACP, 0, in.c_str(), -1, nullptr, 0);
    }
    std::wstring out(static_cast<size_t>(len - 1), L'\0');
    MultiByteToWideChar(codePage, 0, in.c_str(), -1, out.data(), len);
    return out;
}

BSTR makeBstr(const std::string& in) {
    std::wstring wide = widenUtf8(in);
    return SysAllocStringLen(wide.data(), static_cast<UINT>(wide.size()));
}

bool g_comInitialized = false;

void cleanupPlatformString(BSTR value) {
    SysFreeString(value);
}

IBlackmagicRawFactory* createFactory() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (hr == S_OK || hr == S_FALSE) {
        g_comInitialized = true;
    } else if (hr != RPC_E_CHANGED_MODE) {
        std::fprintf(stderr, "CoInitializeEx failed (hr=0x%x)\n", hr);
        return nullptr;
    }

    BSTR libraryPath = SysAllocString(
        L"C:\\Program Files (x86)\\Blackmagic Design\\Blackmagic RAW\\"
        L"Blackmagic RAW SDK\\Win\\Libraries");
    IBlackmagicRawFactory* factory =
        CreateBlackmagicRawFactoryInstanceFromPath(libraryPath);
    SysFreeString(libraryPath);
    return factory;
}

void cleanupPlatform() {
    if (g_comInitialized)
        CoUninitialize();
}

#define POPEN _popen
#define PCLOSE _pclose
#define POPEN_MODE "wb"
#else
void cleanupPlatformString(CFStringRef value) {
    CFRelease(value);
}

IBlackmagicRawFactory* createFactory() {
    CFStringRef cfFrameworkPath = CFSTR(
        "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries");
    return CreateBlackmagicRawFactoryInstanceFromPath(cfFrameworkPath);
}

void cleanupPlatform() {}

#define POPEN popen
#define PCLOSE pclose
#define POPEN_MODE "w"
#endif

void shellEscape(const std::string& in, std::string& out) {
    out.clear();
    out.reserve(in.size() + 2);
    out.push_back('"');
    for (char c : in) {
        if (c == '"' || c == '\\' || c == '$' || c == '`') out.push_back('\\');
        out.push_back(c);
    }
    out.push_back('"');
}

#ifdef _WIN32
std::string platformStringToStd(BSTR s) {
    if (s == nullptr) return {};
    UINT len = SysStringLen(s);
    int needed = WideCharToMultiByte(CP_UTF8, 0, s, len, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(needed), '\0');
    WideCharToMultiByte(CP_UTF8, 0, s, len, out.data(), needed, nullptr, nullptr);
    return out;
}

void releasePlatformTimecode(BSTR s) {
    if (s != nullptr) SysFreeString(s);
}
#else
std::string platformStringToStd(CFStringRef s) {
    if (s == nullptr) return {};
    CFIndex len = CFStringGetLength(s);
    CFIndex maxBytes =
        CFStringGetMaximumSizeForEncoding(len, kCFStringEncodingUTF8) + 1;
    std::string out(maxBytes, '\0');
    if (!CFStringGetCString(s, out.data(), maxBytes, kCFStringEncodingUTF8)) {
        return {};
    }
    out.resize(std::char_traits<char>::length(out.c_str()));
    return out;
}

void releasePlatformTimecode(CFStringRef s) {
    if (s != nullptr) CFRelease(s);
}
#endif

bool isValidTimecode(const std::string& tc) {
    // Accept HH:MM:SS:FF or HH:MM:SS;FF (drop-frame). Anything else is
    // rejected so we don't pass garbage to ffmpeg.
    if (tc.size() != 11) return false;
    for (size_t i = 0; i < tc.size(); ++i) {
        if (i == 2 || i == 5 || i == 8) {
            if (tc[i] != ':' && tc[i] != ';') return false;
        } else {
            if (tc[i] < '0' || tc[i] > '9') return false;
        }
    }
    return true;
}

}  // namespace

int main(int argc, const char* argv[]) {
    if (argc < 3 || argc > 4) {
        std::fprintf(stderr,
                     "usage: %s <input.braw> <output.mp4> [bitrate]\n",
                     argv[0]);
        return 2;
    }
    const std::string inputPath = argv[1];
    const std::string outputPath = argv[2];
    const std::string bitrate = (argc >= 4) ? argv[3] : "50M";

#ifdef _WIN32
    BSTR clipPath = makeBstr(inputPath);
#else
    CFStringRef cfInput = CFStringCreateWithCString(
        nullptr, inputPath.c_str(), kCFStringEncodingUTF8);
#define clipPath cfInput
#endif

    IBlackmagicRawFactory* factory = createFactory();
    if (factory == nullptr) {
        std::fprintf(stderr, "failed to load BRAW SDK framework\n");
        cleanupPlatformString(clipPath);
        cleanupPlatform();
        return 3;
    }

    IBlackmagicRaw* codec = nullptr;
    HRESULT hr = factory->CreateCodec(&codec);
    if (hr != S_OK) {
        std::fprintf(stderr, "CreateCodec failed (hr=0x%x)\n", hr);
        factory->Release();
        cleanupPlatformString(clipPath);
        cleanupPlatform();
        return 4;
    }

    IBlackmagicRawClip* clip = nullptr;
    hr = codec->OpenClip(clipPath, &clip);
    if (hr != S_OK) {
        std::fprintf(stderr, "OpenClip failed for %s (hr=0x%x)\n",
                     inputPath.c_str(), hr);
        codec->Release();
        factory->Release();
        cleanupPlatformString(clipPath);
        cleanupPlatform();
        return 5;
    }

    uint32_t width = 0, height = 0;
    float fps = 0.0f;
    uint64_t frameCount = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);
    clip->GetFrameRate(&fps);
    clip->GetFrameCount(&frameCount);

    std::string startTimecode;
#ifdef _WIN32
    BSTR tcRef = nullptr;
#else
    CFStringRef tcRef = nullptr;
#endif
    if (clip->GetTimecodeForFrame(0, &tcRef) == S_OK && tcRef != nullptr) {
        startTimecode = platformStringToStd(tcRef);
        releasePlatformTimecode(tcRef);
    }

    std::fprintf(stderr,
                 "clip %s: %ux%u @ %.3f fps, %llu frames, tc=%s\n",
                 inputPath.c_str(), width, height, fps,
                 (unsigned long long)frameCount,
                 startTimecode.empty() ? "<none>" : startTimecode.c_str());

    FrameSlot slot;
    SyncCallback cb;
    cb.slot = &slot;
    hr = codec->SetCallback(&cb);
    if (hr != S_OK) {
        std::fprintf(stderr, "SetCallback failed (hr=0x%x)\n", hr);
        clip->Release();
        codec->Release();
        factory->Release();
        cleanupPlatformString(clipPath);
        cleanupPlatform();
        return 6;
    }

    std::string quotedOut;
    shellEscape(outputPath, quotedOut);
    std::string tcArg;
    if (!startTimecode.empty() && isValidTimecode(startTimecode)) {
        std::string quotedTc;
        shellEscape(startTimecode, quotedTc);
        tcArg = " -timecode " + quotedTc;
    } else if (!startTimecode.empty()) {
        std::fprintf(stderr,
                     "warning: ignoring unparseable timecode %s\n",
                     startTimecode.c_str());
    }
    char ffmpegCmd[2048];
    std::snprintf(ffmpegCmd, sizeof(ffmpegCmd),
                  "ffmpeg -hide_banner -loglevel error -y "
                  "-f rawvideo -pix_fmt rgba -s %ux%u -r %.6f -i pipe:0 "
#ifdef _WIN32
                  "-c:v libx265 -preset medium -tag:v hvc1 -b:v %s%s "
#else
                  "-c:v hevc_videotoolbox -tag:v hvc1 -b:v %s%s "
#endif
                  "-movflags +faststart -an %s",
                  width, height, fps, bitrate.c_str(),
                  tcArg.c_str(), quotedOut.c_str());
    std::fprintf(stderr, "ffmpeg: %s\n", ffmpegCmd);

    FILE* ffmpeg = POPEN(ffmpegCmd, POPEN_MODE);
    if (!ffmpeg) {
        std::fprintf(stderr, "popen ffmpeg failed\n");
        clip->Release();
        codec->Release();
        factory->Release();
        cleanupPlatformString(clipPath);
        cleanupPlatform();
        return 7;
    }

    bool ok = true;
    for (uint64_t i = 0; i < frameCount; ++i) {
        IBlackmagicRawJob* readJob = nullptr;
        hr = clip->CreateJobReadFrame(i, &readJob);
        if (hr != S_OK) {
            std::fprintf(stderr, "CreateJobReadFrame failed at %llu (hr=0x%x)\n",
                         (unsigned long long)i, hr);
            ok = false;
            break;
        }
        {
            std::lock_guard<std::mutex> lk(slot.m);
            slot.ready = false;
            slot.result = S_OK;
            slot.data.clear();
        }
        hr = readJob->Submit();
        if (hr != S_OK) {
            std::fprintf(stderr, "readJob->Submit failed at %llu (hr=0x%x)\n",
                         (unsigned long long)i, hr);
            readJob->Release();
            ok = false;
            break;
        }
        std::unique_lock<std::mutex> lk(slot.m);
        slot.cv.wait(lk, [&] { return slot.ready; });
        if (slot.result != S_OK) {
            std::fprintf(stderr, "frame %llu process failed (hr=0x%x)\n",
                         (unsigned long long)i, slot.result);
            ok = false;
            break;
        }
        size_t want = slot.data.size();
        size_t wrote = std::fwrite(slot.data.data(), 1, want, ffmpeg);
        if (wrote != want) {
            std::fprintf(stderr, "ffmpeg pipe short write at frame %llu\n",
                         (unsigned long long)i);
            ok = false;
            break;
        }
        if (i % 30 == 0 || i + 1 == frameCount) {
            std::fprintf(stderr, "  frame %llu/%llu\r",
                         (unsigned long long)(i + 1),
                         (unsigned long long)frameCount);
            std::fflush(stderr);
        }
    }
    std::fprintf(stderr, "\n");

    int ffmpegRc = PCLOSE(ffmpeg);
    codec->FlushJobs();

    clip->Release();
    codec->Release();
    factory->Release();
    cleanupPlatformString(clipPath);
    cleanupPlatform();

    if (!ok) return 8;
    if (ffmpegRc != 0) {
        std::fprintf(stderr, "ffmpeg exited rc=%d\n", ffmpegRc);
        return 9;
    }
    return 0;
}
