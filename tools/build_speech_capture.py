#!/usr/bin/env python3
"""Build the local macOS audio helper. This command does not request audio permissions or record."""
import argparse
import plistlib
from pathlib import Path
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,default=Path.home()/'.local/share/assistant-stack/runtime/SpeechDatasetCapture.app')
args=parser.parse_args()
contents=args.output/'Contents';binary=contents/'MacOS/SpeechCapture';binary.parent.mkdir(parents=True,exist_ok=True)
info={'CFBundleIdentifier':'local.assistant.speech-dataset-capture','CFBundleName':'Speech Dataset Capture',
      'CFBundleExecutable':'SpeechCapture','CFBundlePackageType':'APPL','CFBundleVersion':'1','LSUIElement':True,
      'NSMicrophoneUsageDescription':'Запись диктовки и вашей речи на встречах для персонального датасета ASR.',
      'NSAudioCaptureUsageDescription':'Запись звука встречи для персонального датасета ASR.'}
(contents/'Info.plist').write_bytes(plistlib.dumps(info))
source=Path(__file__).parent/'macos/SpeechCapture.swift'
subprocess.run(['swiftc','-swift-version','5','-O','-framework','AppKit','-framework','AVFoundation','-framework','CoreAudio',str(source),'-o',str(binary)],check=True)
subprocess.run(['codesign','--force','--sign','-','--identifier',info['CFBundleIdentifier'],str(args.output)],check=True)
print(binary)
