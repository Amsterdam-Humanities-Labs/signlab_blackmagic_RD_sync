// Transcode a Blackmagic RAW (.braw) clip to H.265 (.mp4) by piping decoded
// RGBA frames into ffmpeg's hevc_videotoolbox encoder.
//
// The BRAW SDK is loaded at runtime from
//   /Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries
// via the dispatch shim shipped in the SDK Include/ folder.

#include "BlackmagicRawAPI.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <condition_variable>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

#include <CoreFoundation/CoreFoundation.h>

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
    void SidecarMetadataParseWarning(IBlackmagicRawClip*, CFStringRef,
                                     uint32_t, CFStringRef) override {}
    void SidecarMetadataParseError(IBlackmagicRawClip*, CFStringRef,
                                   uint32_t, CFStringRef) override {}
    void PreparePipelineComplete(void*, HRESULT) override {}

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID*) override {
        return E_NOTIMPL;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return 0; }
    ULONG STDMETHODCALLTYPE Release() override { return 0; }
};

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

    CFStringRef cfInput = CFStringCreateWithCString(
        nullptr, inputPath.c_str(), kCFStringEncodingUTF8);
    CFStringRef cfFrameworkPath = CFSTR(
        "/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac/Libraries");

    IBlackmagicRawFactory* factory =
        CreateBlackmagicRawFactoryInstanceFromPath(cfFrameworkPath);
    if (factory == nullptr) {
        std::fprintf(stderr, "failed to load BRAW SDK framework\n");
        CFRelease(cfInput);
        return 3;
    }

    IBlackmagicRaw* codec = nullptr;
    HRESULT hr = factory->CreateCodec(&codec);
    if (hr != S_OK) {
        std::fprintf(stderr, "CreateCodec failed (hr=0x%x)\n", hr);
        factory->Release();
        CFRelease(cfInput);
        return 4;
    }

    IBlackmagicRawClip* clip = nullptr;
    hr = codec->OpenClip(cfInput, &clip);
    if (hr != S_OK) {
        std::fprintf(stderr, "OpenClip failed for %s (hr=0x%x)\n",
                     inputPath.c_str(), hr);
        codec->Release();
        factory->Release();
        CFRelease(cfInput);
        return 5;
    }

    uint32_t width = 0, height = 0;
    float fps = 0.0f;
    uint64_t frameCount = 0;
    clip->GetWidth(&width);
    clip->GetHeight(&height);
    clip->GetFrameRate(&fps);
    clip->GetFrameCount(&frameCount);

    std::fprintf(stderr,
                 "clip %s: %ux%u @ %.3f fps, %llu frames\n",
                 inputPath.c_str(), width, height, fps,
                 (unsigned long long)frameCount);

    FrameSlot slot;
    SyncCallback cb;
    cb.slot = &slot;
    hr = codec->SetCallback(&cb);
    if (hr != S_OK) {
        std::fprintf(stderr, "SetCallback failed (hr=0x%x)\n", hr);
        clip->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfInput);
        return 6;
    }

    std::string quotedOut;
    shellEscape(outputPath, quotedOut);
    char ffmpegCmd[2048];
    std::snprintf(ffmpegCmd, sizeof(ffmpegCmd),
                  "ffmpeg -hide_banner -loglevel error -y "
                  "-f rawvideo -pix_fmt rgba -s %ux%u -r %.6f -i pipe:0 "
                  "-c:v hevc_videotoolbox -tag:v hvc1 -b:v %s "
                  "-movflags +faststart -an %s",
                  width, height, fps, bitrate.c_str(), quotedOut.c_str());
    std::fprintf(stderr, "ffmpeg: %s\n", ffmpegCmd);

    FILE* ffmpeg = popen(ffmpegCmd, "w");
    if (!ffmpeg) {
        std::fprintf(stderr, "popen ffmpeg failed\n");
        clip->Release();
        codec->Release();
        factory->Release();
        CFRelease(cfInput);
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

    int ffmpegRc = pclose(ffmpeg);
    codec->FlushJobs();

    clip->Release();
    codec->Release();
    factory->Release();
    CFRelease(cfInput);

    if (!ok) return 8;
    if (ffmpegRc != 0) {
        std::fprintf(stderr, "ffmpeg exited rc=%d\n", ffmpegRc);
        return 9;
    }
    return 0;
}
