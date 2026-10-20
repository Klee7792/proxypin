//
//  PacketTunnelProvider.swift
//  ProxyPin
//
//  Created by 汪红恩 on 2023/7/4.
//

import NetworkExtension
import Network
import os.log

class PacketTunnelProvider: NEPacketTunnelProvider {
    private var proxyVpnService: ProxyVpnService?

    private static func isIPv4Address(_ value: String) -> Bool {
        let parts = value.split(separator: ".")
        guard parts.count == 4 else {
            return false
        }
        return parts.allSatisfy { part in
            guard let number = Int(part), number >= 0, number <= 255 else {
                return false
            }
            return String(number) == part
        }
    }

    private static func makeError(_ message: String) -> NSError {
        return NSError(domain: "ProxyPin.PacketTunnelProvider", code: -1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    override func startTunnel(options: [String : NSObject]?, completionHandler: @escaping (Error?) -> Void) {
        NSLog("startTunnel")

        guard let tunnelProtocol = protocolConfiguration as? NETunnelProviderProtocol else {
            let error = Self.makeError("Invalid tunnel protocol configuration")
            NSLog("[ERROR] \(error.localizedDescription)")
            completionHandler(error)
            return
        }
        guard let conf = tunnelProtocol.providerConfiguration else {
            let error = Self.makeError("No ProtocolConfiguration Found")
            NSLog("[ERROR] \(error.localizedDescription)")
            completionHandler(error)
            return
        }

        guard let host = conf["proxyHost"] as? String, !host.isEmpty else {
            let error = Self.makeError("Missing proxyHost")
            NSLog("[ERROR] \(error.localizedDescription)")
            completionHandler(error)
            return
        }
        guard let proxyPort = conf["proxyPort"] as? Int,
              proxyPort > 0,
              proxyPort <= Int(UInt16.max),
              let nwProxyPort = NWEndpoint.Port(rawValue: UInt16(proxyPort)) else {
            let error = Self.makeError("Invalid proxyPort")
            NSLog("[ERROR] \(error.localizedDescription)")
            completionHandler(error)
            return
        }
        let ipProxy = conf["ipProxy"] as? Bool ?? false

        // parse proxyPassDomains: accept either [String] or comma-separated String
        var proxyPassDomains: [String]? = nil
        if let arr = conf["proxyPassDomains"] as? [String] {
            proxyPassDomains = arr.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
        } else if let csv = conf["proxyPassDomains"] as? String {
            let list = csv.components(separatedBy: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            proxyPassDomains = list.isEmpty ? nil : list
        }

//        let networkSettings = NEPacketTunnelNetworkSettings(tunnelRemoteAddress: "127.0.0.1")
        let networkSettings = NEPacketTunnelNetworkSettings(tunnelRemoteAddress: host)
        NSLog(conf.debugDescription)

        networkSettings.mtu = 1500

        let ipv4Settings = NEIPv4Settings(addresses: ["10.0.0.2"], subnetMasks: ["255.255.255.255"])

        if (ipProxy){
            ipv4Settings.includedRoutes = [NEIPv4Route.default()]
            var excludedRoutes = [
                NEIPv4Route(destinationAddress: "127.0.0.0", subnetMask: "255.0.0.0"),
                NEIPv4Route(destinationAddress: "169.254.0.0", subnetMask: "255.255.0.0"),
                NEIPv4Route(destinationAddress: "224.0.0.0", subnetMask: "240.0.0.0"),
                NEIPv4Route(destinationAddress: "240.0.0.0", subnetMask: "240.0.0.0"),
            ]
            if Self.isIPv4Address(host) {
                excludedRoutes.append(NEIPv4Route(destinationAddress: host, subnetMask: "255.255.255.255"))
            } else {
                NSLog("[WARN] proxyHost is not an IPv4 address, cannot add excluded route: \(host)")
            }
            ipv4Settings.excludedRoutes = excludedRoutes

        }

        //http代理
        let proxySettings = NEProxySettings()
        proxySettings.httpEnabled = true
        proxySettings.httpServer = NEProxyServer(address: host, port: proxyPort)
        proxySettings.httpsEnabled = true
        proxySettings.httpsServer = NEProxyServer(address: host, port: proxyPort)
        // If a proxyPassDomains list was provided, use it as the exceptionList so these domains bypass the proxy.
        if let pass = proxyPassDomains {
            proxySettings.exceptionList = pass
        }

        proxySettings.matchDomains = [""]
        networkSettings.proxySettings =  proxySettings

        networkSettings.ipv4Settings = ipv4Settings

        setTunnelNetworkSettings(networkSettings) { error in
           guard error == nil else {
               NSLog("startTunnel Encountered an error setting up the network: \(error.debugDescription)")
               completionHandler(error)
               return
           }

           if (ipProxy){
             let proxyAddress =  Network.NWEndpoint.hostPort(host: NWEndpoint.Host(host), port: nwProxyPort)
             self.proxyVpnService = ProxyVpnService(packetFlow: self.packetFlow, proxyAddress: proxyAddress)
             self.proxyVpnService!.start()

             // Local IP-layer capture: start the loopback relay so the Dart proxy's
             // upstream can reach the internet without looping back into the tunnel.
             // Relay port = proxyPort + 1 (mirrors the Dart side).
             let relayPort = UInt16(min(proxyPort + 1, Int(UInt16.max)))
             VpnRelay.shared.start(relayPort: relayPort)
           }
           completionHandler(nil)
       }
    }

    override func stopTunnel(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        proxyVpnService?.stop()
        proxyVpnService = nil
        VpnRelay.shared.stop()
        completionHandler()
    }

    override func handleAppMessage(_ messageData: Data, completionHandler: ((Data?) -> Void)?) {
        // Add code here to handle the message.
        if let handler = completionHandler {
            NSLog("handleAppMessage ", messageData.debugDescription)
            handler(messageData)
        }
    }

    override func sleep(completionHandler: @escaping () -> Void) {
        // Add code here to get ready to sleep.
        completionHandler()
    }

    override func wake() {
        // Add code here to wake up.
    }
}

// MARK: - Loopback HTTP-CONNECT relay
//
// Runs INSIDE the NE provider process. When local IP-layer capture is on, the
// packet tunnel captures ALL device traffic — including ProxyPin's own upstream
// connections — which would loop back into the tunnel. This relay breaks the
// loop: the Dart proxy sends its upstream via `CONNECT host:port` to
// 127.0.0.1:<relayPort>, and the relay dials the real target here. Connections
// made by the provider process bypass its own packet tunnel, so there is no
// loop. Relay port = proxyPort + 1 (mirrors the Dart side:
// lib/network/channel/ip_layer_relay.dart and lib/native/vpn.dart).

final class VpnRelay {
    static let shared = VpnRelay()
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.proxypin.vpnrelay")

    func start(relayPort: UInt16) {
        queue.async { [weak self] in
            self?._start(relayPort: relayPort)
        }
    }

    private func _start(relayPort: UInt16) {
        let params = NWParameters.tcp
        if #available(iOS 13.0, *) {
            params.requiredLocalEndpoint = NWEndpoint.hostPort(
                host: NWEndpoint.Host("127.0.0.1"),
                port: NWEndpoint.Port(rawValue: relayPort)!)
        }
        do {
            let listener = try NWListener(using: params)
            listener.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    NSLog("[VpnRelay] listening on 127.0.0.1:\(relayPort)")
                case .failed(let err):
                    NSLog("[VpnRelay] listener failed: \(err)")
                default:
                    break
                }
            }
            listener.newConnectionHandler = { [weak self] conn in
                self?.handleIncoming(conn)
            }
            listener.start(queue: queue)
            self.listener = listener
        } catch {
            NSLog("[VpnRelay] failed to start: \(error)")
        }
    }

    func stop() {
        queue.async { [weak self] in
            self?.listener?.cancel()
            self?.listener = nil
            NSLog("[VpnRelay] stopped")
        }
    }

    private func handleIncoming(_ conn: NWConnection) {
        conn.stateUpdateHandler = { _ in }
        conn.start(queue: queue)
        readConnectRequest(conn, buffer: Data())
    }

    private func readConnectRequest(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 4096) { data, _, isComplete, error in
            var buf = buffer
            if let data = data, !data.isEmpty {
                buf.append(data)
            }
            if let idx = Self.indexOfHeaderEnd(buf) {
                let header = buf.subdata(in: 0..<idx)
                let rest = buf.subdata(in: idx..<buf.count)
                self.handleConnect(conn, header: header, rest: rest)
            } else if isComplete || error != nil {
                NSLog("[VpnRelay] incomplete CONNECT request, closing")
                conn.cancel()
            } else {
                self.readConnectRequest(conn, buffer: buf)
            }
        }
    }

    private static func indexOfHeaderEnd(_ data: Data) -> Int? {
        guard data.count >= 4 else { return nil }
        for i in 0...(data.count - 4) {
            if data[i] == 13, data[i + 1] == 10, data[i + 2] == 13, data[i + 3] == 10 {
                return i + 4
            }
        }
        return nil
    }

    private func handleConnect(_ conn: NWConnection, header: Data, rest: Data) {
        guard let text = String(data: header, encoding: .utf8) else { conn.cancel(); return }
        let lines = text.split(separator: "\r\n", omittingEmptySubsequences: false)
        guard !lines.isEmpty else { conn.cancel(); return }
        let parts = lines[0].split(separator: " ", omittingEmptySubsequences: true)
        guard parts.count >= 2, parts[0] == "CONNECT" else { conn.cancel(); return }
        let target = parts[1]
        let comps = target.split(separator: ":")
        guard comps.count == 2, let port = UInt16(comps[1]) else { conn.cancel(); return }
        let host = String(comps[0])
        NSLog("[VpnRelay] CONNECT \(host):\(port)")

        let upstream = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: .tcp)
        upstream.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                let ok = "HTTP/1.1 200 Connection established\r\n\r\n".data(using: .utf8)!
                conn.send(content: ok, contentContext: .defaultMessage, isComplete: false,
                          completion: .contentProcessed({ _ in
                              self?.pipe(client: conn, upstream: upstream, initialClientData: rest)
                          }))
            case .failed(let err):
                NSLog("[VpnRelay] upstream failed: \(err)")
                conn.cancel()
                upstream.cancel()
            default:
                break
            }
        }
        upstream.start(queue: queue)
    }

    private func pipe(client: NWConnection, upstream: NWConnection, initialClientData: Data) {
        if !initialClientData.isEmpty {
            upstream.send(content: initialClientData, contentContext: .defaultMessage, isComplete: false,
                          completion: .contentProcessed({ _ in }))
        }
        pump(from: client, to: upstream)
        pump(from: upstream, to: client)
    }

    private func pump(from: NWConnection, to: NWConnection) {
        from.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, isComplete, error in
            if let data = data, !data.isEmpty {
                to.send(content: data, contentContext: .defaultMessage, isComplete: false,
                        completion: .contentProcessed({ _ in
                            self.pump(from: from, to: to)
                        }))
            } else if isComplete || error != nil {
                to.send(content: nil, contentContext: .defaultMessage, isComplete: true, completion: .no)
                from.cancel()
                to.cancel()
            } else {
                self.pump(from: from, to: to)
            }
        }
    }
}
