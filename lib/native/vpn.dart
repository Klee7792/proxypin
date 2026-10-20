import 'package:flutter/services.dart';
import 'package:proxypin/network/bin/configuration.dart';
import 'package:proxypin/network/channel/ip_layer_relay.dart';
import 'package:proxypin/network/util/logger.dart';

class Vpn {
  static const MethodChannel proxyVpnChannel = MethodChannel('com.proxy/proxyVpn');

  static bool isVpnStarted = false; //vpn是否已经启动

  static List<String> _proxyPassDomains(Configuration configuration) {
    return configuration.proxyPassDomains.split(';').map((e) => e.trim()).where((e) => e.isNotEmpty).toList();
  }

  static void startVpn(String host, int port, Configuration configuration, {bool? ipProxy = false}) {
    List<String>? appList = configuration.appWhitelistEnabled ? configuration.appWhitelist : [];

    List<String>? disallowApps;
    if (appList.isEmpty) {
      disallowApps = configuration.appBlacklist ?? [];
    }

    final proxyPassDomains = _proxyPassDomains(configuration);
    logger.d("Starting VPN with host: $host, port: $port,  proxyPassDomains: $proxyPassDomains");

    // Reset relay state; only re-enable below for local IP-layer capture.
    IpLayerRelay.host = null;
    IpLayerRelay.port = null;

    // Local IP-layer capture: the Dart proxy must route its upstream through the
    // NE extension's loopback relay (port = proxyPort + 1) to break the
    // full-tunnel capture loop. Only meaningful when the proxy is local.
    if (ipProxy == true && host == "127.0.0.1") {
      IpLayerRelay.host = "127.0.0.1";
      IpLayerRelay.port = port + 1;
      logger.d("IP-layer capture relay enabled at 127.0.0.1:${port + 1}");
    }

    proxyVpnChannel.invokeMethod("startVpn", {
      "proxyHost": host,
      "proxyPort": port,
      "allowApps": appList,
      "disallowApps": disallowApps,
      "ipProxy": ipProxy,
      "setSystemProxy": configuration.enableSystemProxy,
      "proxyPassDomains": proxyPassDomains,
    });
    isVpnStarted = true;
  }

  static void stopVpn() {
    IpLayerRelay.host = null;
    IpLayerRelay.port = null;
    proxyVpnChannel.invokeMethod("stopVpn");
    isVpnStarted = false;
  }

  //重启vpn
  static void restartVpn(String host, int port, Configuration configuration, {bool ipProxy = false}) {
    List<String>? appList = configuration.appWhitelistEnabled ? configuration.appWhitelist : [];

    List<String>? disallowApps;
    if (appList.isEmpty) {
      disallowApps = configuration.appBlacklist ?? [];
    }
    final proxyPassDomains = _proxyPassDomains(configuration);
    // Reset relay state; only re-enable below for local IP-layer capture.
    IpLayerRelay.host = null;
    IpLayerRelay.port = null;
    if (ipProxy == true && host == "127.0.0.1") {
      IpLayerRelay.host = "127.0.0.1";
      IpLayerRelay.port = port + 1;
    }
    proxyVpnChannel.invokeMethod("restartVpn", {
      "proxyHost": host,
      "proxyPort": port,
      "allowApps": appList,
      "disallowApps": disallowApps,
      "ipProxy": ipProxy,
      "setSystemProxy": configuration.enableSystemProxy,
      "proxyPassDomains": proxyPassDomains,
    });

    isVpnStarted = true;
  }

  static Future<bool> isRunning() async {
    return await proxyVpnChannel.invokeMethod("isRunning");
  }
}
