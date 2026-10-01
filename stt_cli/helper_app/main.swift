// stt.app — the small application stt needs for two things a Python process cannot do.
//
// 1. A notification with a real Stop button. macOS gives an action button only to a
//    notification posted by an application bundle with its own identifier; `osascript
//    display notification` has no buttons, and `display dialog` takes the keyboard focus the
//    dictated words are being typed into. `notify` posts one, waits for the click and
//    prints what happened on standard output.
// 2. A stable principal for macOS privacy grants. launchd runs the dictation daemon as
//    `stt.app/Contents/MacOS/stt run -- <python> <stt> mic __serve …`. macOS holds the
//    process launchd started responsible for its children, so Accessibility and
//    Microphone are granted to "stt" — this bundle's identifier and signature — instead
//    of to a Homebrew python binary whose path and signature change on every upgrade.
//
// Built from this one file by `stt mic helper` (stt_cli/live/helper.py); nothing here is
// downloaded. The protocol stt relies on is in docs/specs/live-dictation-ux.md, section 6.2.

import AppKit
import Foundation
import UserNotifications

let categoryID = "STT_BURST"
let stopActionID = "STOP"
let requestPrefix = "stt-burst-"

func fail(_ message: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data("stt helper: \(message)\n".utf8))
    exit(code)
}

// Exactly one line on standard output, then exit: what stt reads to decide the burst.
func answer(_ word: String) -> Never {
    FileHandle.standardOutput.write(Data("\(word)\n".utf8))
    exit(0)
}

// MARK: - notify

final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    let center = UNUserNotificationCenter.current()
    let requestID: String
    let title: String
    let body: String
    var sources: [DispatchSourceProtocol] = []

    init(burst: String, title: String, body: String) {
        requestID = requestPrefix + burst
        self.title = title
        self.body = body
    }

    func start() {
        center.delegate = self
        let stop = UNNotificationAction(identifier: stopActionID, title: "Stop", options: [])
        // `customDismissAction` is what makes a dismissal reach the delegate at all; without
        // it a closed notification is indistinguishable from one still on screen.
        let category = UNNotificationCategory(
            identifier: categoryID, actions: [stop], intentIdentifiers: [],
            options: [.customDismissAction])
        center.setNotificationCategories([category])
        withdrawWhenAskedTo()
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            DispatchQueue.main.async {
                if !granted {
                    if let error { FileHandle.standardError.write(Data("stt helper: \(error)\n".utf8)) }
                    answer("DENIED")
                }
                self.post()
            }
        }
    }

    func post() {
        // A helper that was killed with -9 cannot have withdrawn its notification; this is
        // where its stale Stop button goes, before the new one appears.
        center.getDeliveredNotifications { delivered in
            let stale = delivered.map(\.request.identifier).filter { $0.hasPrefix(requestPrefix) }
            self.center.removeDeliveredNotifications(withIdentifiers: stale)
            let content = UNMutableNotificationContent()
            content.title = self.title
            content.body = self.body
            content.categoryIdentifier = categoryID
            content.threadIdentifier = "stt-burst"
            let request = UNNotificationRequest(identifier: self.requestID, content: content, trigger: nil)
            self.center.add(request) { error in
                if let error {
                    FileHandle.standardError.write(Data("stt helper: \(error)\n".utf8))
                    DispatchQueue.main.async { answer("CLOSED") }
                }
            }
        }
    }

    // SIGTERM (stt ending the burst some other way) or the parent disappearing (stt killed)
    // both mean the same: take the Stop button off the screen, then exit.
    func withdrawWhenAskedTo() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { self.withdraw(code: 0) }
            source.resume()
            sources.append(source)
        }
        let parent = getppid()
        if parent > 1 {
            let source = DispatchSource.makeProcessSource(identifier: parent, eventMask: .exit, queue: .main)
            source.setEventHandler { self.withdraw(code: 0) }
            source.resume()
            sources.append(source)
        }
    }

    func withdraw(code: Int32) {
        center.removePendingNotificationRequests(withIdentifiers: [requestID])
        center.removeDeliveredNotifications(withIdentifiers: [requestID])
        // The removal is asynchronous; a round trip on the same connection after it is what
        // makes sure it reached the notification server before this process is gone.
        center.getDeliveredNotifications { _ in exit(code) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { exit(code) }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        completionHandler()
        guard response.notification.request.identifier == requestID else { return }
        switch response.actionIdentifier {
        case stopActionID: answer("STOP")
        case UNNotificationDismissActionIdentifier: answer("DISMISSED")
        default: answer("CLOSED")  // the body was clicked: the notification is gone, no Stop
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }
}

// MARK: - run

// The daemon's wrapper: start the command as a child and stay its parent, so that macOS
// attributes the child's privacy requests to this bundle. `exec` would not do: it replaces
// this image in the same process and macOS would evaluate python again.
func run(_ argv: [String]) -> Never {
    guard let program = argv.first else { fail("run needs a command after --") }
    setenv("STT_HELPER_APP", Bundle.main.bundlePath, 1)
    var cargs: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0) } + [nil]
    var pid: pid_t = 0
    let spawned = posix_spawn(&pid, program, nil, nil, &cargs, environ)
    if spawned != 0 { fail("could not start \(program): \(String(cString: strerror(spawned)))", code: 127) }
    for sig in [SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        source.setEventHandler { kill(pid, sig) }
        source.resume()
        retained.append(source)
    }
    var status: Int32 = 0
    while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
    let signalled = status & 0x7f
    exit(signalled == 0 ? (status >> 8) & 0xff : 128 + signalled)
}

var retained: [DispatchSourceProtocol] = []
// The notification centre holds its delegate weakly; this is what keeps the one that reads
// the Stop click alive for the whole run loop.
var notifier: Notifier?

// MARK: - authorization

// Whether this bundle may post notifications, and in which style, without asking:
// `stt doctor` prints it. Two words: "authorized alert", "denied none", "not-determined none".
func authorization() -> Never {
    UNUserNotificationCenter.current().getNotificationSettings { settings in
        let status: [UNAuthorizationStatus: String] = [
            .notDetermined: "not-determined", .denied: "denied", .authorized: "authorized",
            .provisional: "provisional",
        ]
        let style: [UNAlertStyle: String] = [.none: "none", .banner: "banner", .alert: "alert"]
        answer("\(status[settings.authorizationStatus] ?? "unknown") \(style[settings.alertStyle] ?? "unknown")")
    }
    dispatchMain()
}

// MARK: - main

func option(_ name: String, in args: [String]) -> String? {
    guard let at = args.firstIndex(of: name), at + 1 < args.count else { return nil }
    return args[at + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "run":
    let rest = Array(args.dropFirst())
    run(rest.first == "--" ? Array(rest.dropFirst()) : rest)
case "notify":
    guard let burst = option("--burst", in: args), let body = option("--body", in: args) else {
        fail("usage: stt notify --burst ID [--title TEXT] --body TEXT")
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    notifier = Notifier(burst: burst, title: option("--title", in: args) ?? "stt", body: body)
    notifier?.start()
    app.run()
case "authorization":
    authorization()
default:
    // Opened from Finder, or by macOS delivering a click on a notification whose helper is
    // already gone: that burst has ended, so there is nothing to stop. Say what this is.
    fail("this is stt's helper; it is started by stt, not opened (see `stt mic helper`)")
}
