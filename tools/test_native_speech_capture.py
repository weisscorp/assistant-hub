#!/usr/bin/env python3
"""Exercise the exact Track code with synthetic PCM; no microphone or screen permission."""
from pathlib import Path
import subprocess,tempfile,shutil
source=(Path(__file__).parent/'macos/SpeechCapture.swift').read_text().split('@available(macOS 15.0, *)')[0]
test=r'''
let folder=URL(fileURLWithPath:CommandLine.arguments[1])
let track=Track("mic",folder)
let format=AVAudioFormat(commonFormat:.pcmFormatFloat32,sampleRate:48000,channels:2,interleaved:true)!
var description:CMAudioFormatDescription?
precondition(CMAudioFormatDescriptionCreate(allocator:kCFAllocatorDefault,asbd:format.streamDescription,layoutSize:0,layout:nil,magicCookieSize:0,magicCookie:nil,extensions:nil,formatDescriptionOut:&description)==noErr)
let values=(0..<96000).map {Float(sin(Double($0)*0.1)*0.12)}
var block:CMBlockBuffer?
precondition(CMBlockBufferCreateWithMemoryBlock(allocator:kCFAllocatorDefault,memoryBlock:nil,blockLength:values.count*4,blockAllocator:kCFAllocatorDefault,customBlockSource:nil,offsetToData:0,dataLength:values.count*4,flags:0,blockBufferOut:&block)==noErr)
values.withUnsafeBytes {bytes in precondition(CMBlockBufferReplaceDataBytes(with:bytes.baseAddress!,blockBuffer:block!,offsetIntoDestination:0,dataLength:bytes.count)==noErr)}
var timing=CMSampleTimingInfo(duration:CMTime(value:1,timescale:48000),presentationTimeStamp:.zero,decodeTimeStamp:.invalid)
var sample:CMSampleBuffer?
precondition(CMSampleBufferCreateReady(allocator:kCFAllocatorDefault,dataBuffer:block,formatDescription:description,sampleCount:48000,sampleTimingEntryCount:1,sampleTimingArray:&timing,sampleSizeEntryCount:0,sampleSizeArray:nil,sampleBufferOut:&sample)==noErr)
try track.append(sample!)
let snapshot=Process();snapshot.executableURL=URL(fileURLWithPath:CommandLine.arguments[2])
snapshot.arguments=["-hide_banner","-loglevel","error","-ignore_length","1","-i",folder.appendingPathComponent("mic-000001.part.wav").path,"-f","f32le","-ac","1","-ar","16000","pipe:1"]
let pipe=Pipe();snapshot.standardOutput=pipe;try snapshot.run()
let previewData=pipe.fileHandleForReading.readDataToEndOfFile();snapshot.waitUntilExit()
precondition(snapshot.terminationStatus==0 && previewData.count>48000,"Growing WAV snapshot unavailable")
print("PASS: growing WAV can feed live transcription before finalization")
let rawSnapshot=Process();rawSnapshot.executableURL=URL(fileURLWithPath:CommandLine.arguments[2])
rawSnapshot.arguments=["-hide_banner","-loglevel","error","-i",folder.appendingPathComponent("mic-000001.part.caf").path,"-f","f32le","-ac","2","-ar","16000","pipe:1"]
let rawPipe=Pipe();rawSnapshot.standardOutput=rawPipe;try rawSnapshot.run()
let stereoSnapshot=rawPipe.fileHandleForReading.readDataToEndOfFile();rawSnapshot.waitUntilExit()
precondition(rawSnapshot.terminationStatus==0 && stereoSnapshot.count>96000,"Growing stereo CAF snapshot unavailable")
print("PASS: growing native CAF supplies stereo echo reference before finalization")
track.finish()
let raw=try AVAudioFile(forReading:folder.appendingPathComponent("mic-000001.raw.caf"),commonFormat:.pcmFormatFloat32,interleaved:true)
precondition(raw.fileFormat.sampleRate==48000 && raw.fileFormat.channelCount==2 && raw.length==48000)
let pcm=AVAudioPCMBuffer(pcmFormat:raw.processingFormat,frameCapacity:4096)!
var offset=0
while raw.framePosition<raw.length {
    try raw.read(into:pcm)
    precondition(pcm.frameLength>0)
    let stored=pcm.floatChannelData![0]
    for i in 0..<Int(pcm.frameLength)*2 {precondition(stored[i]==values[offset+i],"Native PCM changed at \(offset+i)")}
    offset += Int(pcm.frameLength)*2
}
precondition(offset==values.count)
let derived=try AVAudioFile(forReading:folder.appendingPathComponent("mic-000001.ready.wav"))
precondition(derived.fileFormat.sampleRate==16000 && derived.fileFormat.channelCount==1)
precondition(abs(derived.length-16000) <= 1)
print("PASS: original 48kHz stereo PCM preserved exactly; independent 16kHz mono derivative")
'''
with tempfile.TemporaryDirectory(prefix='synthetic-speech-') as temporary:
    root=Path(temporary);swift=root/'Test.swift';swift.write_text(source+test)
    subprocess.run(['swiftc','-swift-version','5',str(swift),'-o',str(root/'test')],check=True)
    subprocess.run([str(root/'test'),str(root),shutil.which('ffmpeg')],check=True)
