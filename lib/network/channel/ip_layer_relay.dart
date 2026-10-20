import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

/// Loopback HTTP-CONNECT relay for local IP-layer capture.
///
/// When IP-layer global capture is enabled the NE packet tunnel captures ALL
/// device traffic — including the ProxyPin app's own upstream connections —
/// which would loop straight back into the tunnel (the build #3 "all red" bug).
///
/// To break the loop the Dart proxy routes its upstream through a relay that
/// lives INSIDE the iOS NetworkExtension process. Connections made by the
/// provider process bypass its own packet tunnel, so the relay reaches the real
/// internet without looping. Because 127.0.0.0/8 is in the tunnel's
/// `excludedRoutes`, traffic from the app to the relay is delivered directly
/// (never re-captured).
///
/// The relay port is `proxyPort + 1` (mirrored in
/// `ios/ProxyPin/vpn/VpnRelay.swift`), so it tracks the local proxy port.
class IpLayerRelay {
  static String? host;
  static int? port;

  static bool get enabled => host != null && port != null;

  static String _connectRequest(String targetHost, int targetPort) =>
      'CONNECT $targetHost:$targetPort HTTP/1.1\r\n'
      'Host: $targetHost:$targetPort\r\n'
      'Proxy-Connection: keep-alive\r\n\r\n';

  /// Plain TCP tunnel through the relay. Returns a [Socket] already tunneled to
  /// [targetHost]:[targetPort]; the caller may layer TLS on top of it.
  static Future<Socket> connect(String targetHost, int targetPort,
      {Duration timeout = const Duration(seconds: 3)}) async {
    final socket = await Socket.connect(host!, port!, timeout: timeout);
    if (socket.address.type != InternetAddressType.unix) {
      socket.setOption(SocketOption.tcpNoDelay, true);
    }
    socket.write(_connectRequest(targetHost, targetPort));
    await _readConnectResponse(socket);
    return socket;
  }

  static Future<void> _readConnectResponse(Socket socket) async {
    final buffer = <int>[];
    final completer = Completer<void>();
    StreamSubscription? sub;
    var done = false;
    void fail(Object e, [StackTrace? st]) {
      if (done) return;
      done = true;
      sub?.cancel();
      completer.completeError(e, st);
    }

    sub = socket.listen((chunk) {
      buffer.addAll(chunk);
      _checkResponse(buffer, onOk: () {
        done = true;
        sub?.cancel();
        completer.complete();
      }, onFail: fail);
    }, onError: fail, onDone: () => fail('Relay closed before CONNECT response'));
    await completer.future;
  }

  static void _checkResponse(List<int> buffer, {void Function()? onOk, void Function(Object, [StackTrace?])? onFail}) {
    if (buffer.length < 4) return;
    final n = buffer.length;
    if (buffer[n - 4] == 13 && buffer[n - 3] == 10 && buffer[n - 2] == 13 && buffer[n - 1] == 10) {
      final resp = String.fromCharCodes(buffer);
      if (resp.startsWith(RegExp(r'HTTP/1\.[01] 2'))) {
        onOk?.call();
      } else {
        onFail?.call('Relay CONNECT failed: $resp');
      }
    }
  }
}
