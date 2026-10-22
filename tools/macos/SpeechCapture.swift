import Foundation
import AppKit
import AVFoundation
import CoreAudio
import AudioToolbox
import CoreMedia

func event(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value), let string = String(data: data, encoding: .utf8) { print(string); fflush(stdout) }
}

func captureError(_ error: Error) {
    let ns = error as NSError
    var value: [String: Any] = ["event":"error", "message":error.localizedDescription, "code":ns.code, "domain":ns.domain]
    if ns.domain == "SystemAudioPermission" {
        value["message"] = "Разрешите запись системного звука в macOS"
        value["permissionPane"] = "audioCapture"
    } else if ns.domain == "SpeechCaptureMicrophone" {
        value["message"] = "Разрешите доступ к микрофону в macOS"
        value["permissionPane"] = "microphone"
    }
    event(value)
}

final class Track {
    let name: String, directory: URL
    let lock = NSLock()
    let output = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
    var converter: AVAudioConverter?
    var file: AVAudioFile?
    var frames: AVAudioFramePosition = 0
    var index = 0
    var path: URL?
    var rawFile: AVAudioFile?
    var rawPath: URL?
    var nativeFormat: AVAudioFormat?
    var lastMeter = 0.0
    init(_ name: String, _ directory: URL) { self.name = name; self.directory = directory }
    func append(_ sample: CMSampleBuffer) throws {
        guard let description = CMSampleBufferGetFormatDescription(sample),
              let basic = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let format = AVAudioFormat(streamDescription: basic) else { return }
        let count = AVAudioFrameCount(CMSampleBufferGetNumSamples(sample))
        guard count > 0, let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: count) else { return }
        input.frameLength = count
        let result = CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, at: 0, frameCount: Int32(count), into: input.mutableAudioBufferList)
        guard result == noErr else { throw NSError(domain: "AudioCopy", code: Int(result)) }
        try appendPCM(input)
    }
    func appendPCM(_ input: AVAudioPCMBuffer) throws {
        lock.lock(); defer { lock.unlock() }
        let format=input.format, count=input.frameLength
        guard count>0 else {return}
        if let nativeFormat, nativeFormat != format { try finishLocked() }
        if rawFile == nil {
            rawPath = directory.appendingPathComponent(String(format: "%@-%06d.part.caf", name, index+1))
            rawFile = try AVAudioFile(forWriting: rawPath!, settings: format.settings, commonFormat: format.commonFormat, interleaved: format.isInterleaved)
            nativeFormat = format
        }
        try rawFile!.write(from: input) // Native rate/channels, no VAD, denoising or silence removal.
        if converter == nil || converter!.inputFormat != format { converter = AVAudioConverter(from: format, to: output) }
        guard let converter, let converted = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: AVAudioFrameCount(Double(count)*16000/format.sampleRate)+64) else { return }
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: converted, error: &error) { _, state in
            if consumed { state.pointee = .noDataNow; return nil }
            consumed = true; state.pointee = .haveData; return input
        }
        if status == .error { throw error ?? NSError(domain: "AudioConvert", code: 1) }
        if converted.frameLength == 0 { return }
        let now = Date.timeIntervalSinceReferenceDate
        if now-lastMeter >= 0.25, let samples = converted.floatChannelData?[0] {
            lastMeter = now
            let count = Int(converted.frameLength)
            let bins = (0..<32).map { bin -> Double in
                let from = bin*count/32, to = max(from+1,(bin+1)*count/32)
                var power:Double = 0
                for i in from..<min(to,count) {power += Double(samples[i]*samples[i])}
                return min(1,sqrt(power/Double(max(1,to-from)))*6)
            }
            event(["event":"meter","track":name,"levels":bins])
        }
        if file == nil {
            index += 1
            path = directory.appendingPathComponent(String(format: "%@-%06d.part.wav", name, index))
            let settings: [String: Any] = [AVFormatIDKey:kAudioFormatLinearPCM, AVSampleRateKey:16000, AVNumberOfChannelsKey:1,
                AVLinearPCMBitDepthKey:16, AVLinearPCMIsFloatKey:false, AVLinearPCMIsBigEndianKey:false]
            file = try AVAudioFile(forWriting: path!, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
            frames = 0
        }
        try file!.write(from: converted); frames += AVAudioFramePosition(converted.frameLength)
        if frames >= 16000*15 { try finishLocked() }
    }
    func finishLocked() throws {
        if let converter, let file, let tail = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: 4096) {
            var error: NSError?
            let status = converter.convert(to: tail, error: &error) { _, state in state.pointee = .endOfStream;return nil }
            if status == .error {throw error ?? NSError(domain: "AudioConvertFinish", code: 1)}
            if tail.frameLength > 0 {try file.write(from: tail);frames += AVAudioFramePosition(tail.frameLength)}
        }
        converter = nil
        if let rawPath {
            rawFile = nil
            let ready = directory.appendingPathComponent(rawPath.lastPathComponent.replacingOccurrences(of: ".part.caf", with: ".raw.caf"))
            try FileManager.default.moveItem(at: rawPath, to: ready)
            event(["event":"raw", "track":name, "path":ready.path, "sampleRate":nativeFormat!.sampleRate, "channels":nativeFormat!.channelCount])
            self.rawPath = nil; nativeFormat = nil
        }
        guard let path else { return }
        file = nil // Finalizes the WAV before making it visible to the importer.
        if frames >= 1600 {
            let ready = directory.appendingPathComponent(path.lastPathComponent.replacingOccurrences(of: ".part.wav", with: ".ready.wav"))
            try FileManager.default.moveItem(at: path, to: ready)
            event(["event":"chunk", "track":name, "path":ready.path, "frames":frames])
        } else { try? FileManager.default.removeItem(at: path) }
        self.path = nil; frames = 0
    }
    func finish() { lock.lock(); defer { lock.unlock() }; try? finishLocked() }
}


func checkAudio(_ status: OSStatus, _ operation: String) throws {
    guard status != noErr else {return}
    let domain = status == OSStatus(0x7065726d) /* perm */ ? "SystemAudioPermission" : "SystemAudio"
    throw NSError(domain:domain,code:Int(status),userInfo:[NSLocalizedDescriptionKey:operation+" ("+String(status)+")"])
}
func readAudio<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) throws {
    var address=AudioObjectPropertyAddress(mSelector:selector,mScope:kAudioObjectPropertyScopeGlobal,mElement:kAudioObjectPropertyElementMain)
    var size=UInt32(MemoryLayout<T>.size)
    try checkAudio(AudioObjectGetPropertyData(object,&address,0,nil,&size,&value),"Не удалось получить формат звука")
}

@available(macOS 15.0, *)
final class SystemTap {
    var tap: AudioObjectID=0
    var aggregate: AudioObjectID=0
    var io: AudioDeviceIOProcID?
    let queue=DispatchQueue(label:"speech-capture.system")
    func start(_ track: Track) throws {
        let description=CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.uuid=UUID();description.name="Speech Dataset Audio";description.isPrivate=true;description.muteBehavior = .unmuted
        try checkAudio(AudioHardwareCreateProcessTap(description,&tap),"Не удалось открыть системный звук")
        var output:AudioObjectID=0
        try readAudio(AudioObjectID(kAudioObjectSystemObject),kAudioHardwarePropertyDefaultOutputDevice,&output)
        var uid:CFString="" as CFString
        try readAudio(output,kAudioDevicePropertyDeviceUID,&uid)
        var basic=AudioStreamBasicDescription()
        try readAudio(tap,kAudioTapPropertyFormat,&basic)
        guard let format=AVAudioFormat(streamDescription:&basic) else {throw NSError(domain:"SystemAudio",code:1)}
        let settings:[String:Any]=[
            kAudioAggregateDeviceNameKey:"Speech Dataset Audio",kAudioAggregateDeviceUIDKey:UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey:true,kAudioAggregateDeviceIsStackedKey:false,
            kAudioAggregateDeviceMainSubDeviceKey:uid,kAudioAggregateDeviceTapAutoStartKey:true,
            kAudioAggregateDeviceSubDeviceListKey:[[kAudioSubDeviceUIDKey:uid]],
            kAudioAggregateDeviceTapListKey:[[kAudioSubTapUIDKey:description.uuid.uuidString,kAudioSubTapDriftCompensationKey:true]]]
        try checkAudio(AudioHardwareCreateAggregateDevice(settings as CFDictionary,&aggregate),"Не удалось подключить аудиодорожку")
        try checkAudio(AudioDeviceCreateIOProcIDWithBlock(&io,aggregate,queue) { _,input,_,_,_ in
            do {
                if let pcm=AVAudioPCMBuffer(pcmFormat:format,bufferListNoCopy:input,deallocator:nil) {try track.appendPCM(pcm)}
            } catch {captureError(error);exit(1)}
        },"Не удалось открыть аудиопоток")
        try checkAudio(AudioDeviceStart(aggregate,io),"Не удалось начать запись системного звука")
    }
    func stop() {
        if aggregate != 0 {
            _ = AudioDeviceStop(aggregate,io)
            if let io {_ = AudioDeviceDestroyIOProcID(aggregate,io)}
            _ = AudioHardwareDestroyAggregateDevice(aggregate);aggregate=0;io=nil
        }
        if tap != 0 {_ = AudioHardwareDestroyProcessTap(tap);tap=0}
    }
    deinit {stop()}
}

@available(macOS 15.0, *)
final class Capture {
    let mic:Track, system:Track, sources:Set<String>
    let engine=AVAudioEngine(), tap=SystemTap()
    var micInstalled=false, stopped=false
    init(directory:URL,sources:Set<String>) {self.sources=sources;mic=Track("mic",directory);system=Track("system",directory)}
    func start() async throws {
        if sources.contains("system") {try tap.start(system)}
        if sources.contains("mic") {
            guard await AVCaptureDevice.requestAccess(for:.audio) else {throw NSError(domain:"SpeechCaptureMicrophone",code:1)}
            let input=engine.inputNode;let format=input.outputFormat(forBus:0)
            guard format.sampleRate>0 && format.channelCount>0 else {throw NSError(domain:"MicrophoneUnavailable",code:1)}
            input.installTap(onBus:0,bufferSize:4096,format:format) { [mic] pcm,_ in
                do {try mic.appendPCM(pcm)} catch {captureError(error);exit(1)}
            }
            micInstalled=true;try engine.start()
        }
        event(["event":"started","sources":Array(sources).sorted(),"capture":"audio-only"])
    }
    func stop() async {
        if stopped {return};stopped=true
        engine.stop();if micInstalled {engine.inputNode.removeTap(onBus:0);micInstalled=false}
        tap.stop();mic.finish();system.finish();event(["event":"stopped"])
    }
}

if #available(macOS 15.0, *) {
    let args=CommandLine.arguments
    NSApplication.shared.setActivationPolicy(.accessory)
    if args.count == 3 && args[1] == "--permission-status" {
        Task {
            // macOS has no public preflight API for Core Audio tap permission.
            let status:[String:Any]=["audioCapture":NSNull(),"microphone":AVCaptureDevice.authorizationStatus(for:.audio) == .authorized,"checkedAt":Date().timeIntervalSince1970]
            let path=URL(fileURLWithPath:args[2])
            try JSONSerialization.data(withJSONObject:status).write(to:path,options:.atomic)
            try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:path.path);exit(0)
        }
        RunLoop.main.run();exit(0)
    }
    if args.count == 2 && args[1] == "--permissions" {
        NSApplication.shared.activate(ignoringOtherApps:true)
        Task {
            let description=CATapDescription(stereoGlobalTapButExcludeProcesses:[])
            description.name="Speech Dataset Audio";description.isPrivate=true
            var tap:AudioObjectID=0
            let status=AudioHardwareCreateProcessTap(description,&tap)
            if status == noErr {_ = AudioHardwareDestroyProcessTap(tap)}
            _ = await AVCaptureDevice.requestAccess(for:.audio);exit(0)
        }
        RunLoop.main.run();exit(0)
    }
    guard args.count == 3 else {event(["event":"error","message":"Usage: SpeechCapture DIRECTORY mic,system"]);exit(2)}
    let directory=URL(fileURLWithPath:args[1]);let sources=Set(args[2].split(separator:",").map(String.init))
    guard !sources.isEmpty && sources.isSubset(of:["mic","system"]) else {exit(2)}
    try FileManager.default.createDirectory(at:directory,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
    let capture=Capture(directory:directory,sources:sources)
    signal(SIGTERM,SIG_IGN);signal(SIGINT,SIG_IGN)
    let stopSignal=DispatchSource.makeSignalSource(signal:SIGTERM,queue:.main)
    stopSignal.setEventHandler {Task {await capture.stop();exit(0)}};stopSignal.resume()
    let interrupt=DispatchSource.makeSignalSource(signal:SIGINT,queue:.main)
    interrupt.setEventHandler {Task {await capture.stop();exit(0)}};interrupt.resume()
    Task {do {try await capture.start()} catch {captureError(error);await capture.stop();exit(1)}}
    RunLoop.main.run()
} else {event(["event":"error","message":"macOS 15+ required"]);exit(2)}
