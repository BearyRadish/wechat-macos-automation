// wincap.swift — capture a specific window by owner name using ScreenCaptureKit
// Usage: wincap <ownerNameSubstring> <outPath> [cropX cropY cropW cropH]
//
// Why: `screencapture -R x,y,w,h` captures whatever is ON TOP at those
// screen coordinates. If another app overlaps the WeChat window, the OCR
// reads the WRONG app's pixels. This tool uses ScreenCaptureKit to grab
// the WeChat window's own content — immune to z-order and overlaps.
//
// Optional crop args (window-relative pixels, in the window's own scale):
//   cropX cropY cropW cropH
// Used to grab just the chat-title band without re-implementing OCR here.

import Cocoa
import Foundation
import ScreenCaptureKit
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: wincap <ownerNameSubstring> <outPath> [cropX cropY cropW cropH]\n".data(using: .utf8)!)
    exit(2)
}
let ownerMatch = args[1].lowercased()
let outPath = args[2]

var crop: CGRect? = nil
if args.count >= 7 {
    let x = Double(args[3])!, y = Double(args[4])!
    let w = Double(args[5])!, h = Double(args[6])!
    crop = CGRect(x: x, y: y, width: w, height: h)
}

let semaphore = DispatchSemaphore(value: 0)
var exitCode: Int32 = 0

// CRITICAL: NSApplication.shared must be touched BEFORE any ScreenCaptureKit
// call, otherwise CGS_REQUIRE_INIT aborts the process ("Assertion failed:
// (did_initialize)"). Accessing it registers the process with the window server.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: true)

        // Prefer the largest matching on-screen window.
        var target: SCWindow? = nil
        var bestArea: Double = 0
        for win in content.windows {
            let app = win.owningApplication
            let bundleId = (app?.bundleIdentifier ?? "").lowercased()
            let appName = (app?.applicationName ?? "").lowercased()
            let winTitle = (win.title ?? "").lowercased()
            let isWeChat = bundleId.contains("wechat")
            let matches = isWeChat || winTitle.contains(ownerMatch) || appName.contains(ownerMatch)
            guard matches else { continue }
            let a = Double(win.frame.width) * Double(win.frame.height)
            if a > bestArea {
                bestArea = a
                target = win
            }
        }

        guard let window = target else {
            FileHandle.standardError.write("ERROR: no on-screen window matching '\(ownerMatch)'\n".data(using: .utf8)!)
            exitCode = 4
            semaphore.signal()
            return
        }

        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.showsCursor = false
        config.shouldBeOpaque = true
        config.captureResolution = .best
        if let c = crop {
            config.sourceRect = c
        }

        let image = try await SCScreenshotManager.captureImage(
            contentFilter: filter, configuration: config)

        let rep = NSBitmapImageRep(cgImage: image)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write("ERROR: PNG encode failed\n".data(using: .utf8)!)
            exitCode = 7
            semaphore.signal()
            return
        }
        try png.write(to: URL(fileURLWithPath: outPath))
        print(outPath)
        semaphore.signal()
    } catch {
        FileHandle.standardError.write("ERROR: \(error)\n".data(using: .utf8)!)
        exitCode = 6
        semaphore.signal()
    }
}

semaphore.wait()
exit(exitCode)
