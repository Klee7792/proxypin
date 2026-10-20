import 'dart:io';

import 'package:flutter/services.dart';

/// 跨应用悬浮窗（巨魔/越狱）
///
/// 通过原生 UIWindow（高 windowLevel + springboard.overlay 授权）在 SpringBoard
/// 与其它 App 之上显示请求列表。未越狱的纯巨魔设备上该授权不被 AMFI 放行，
/// 浮层会退化为仅在 App 内显示。
class GlobalFloatingWindow {
  static const MethodChannel _channel = MethodChannel('com.proxy/globalFloatingWindow');

  /// 切换显示/隐藏，返回切换后是否可见。
  static Future<bool> toggle() async {
    if (Platform.isAndroid) return false;
    final bool? visible = await _channel.invokeMethod('toggle');
    return visible ?? false;
  }

  static Future<void> show() async {
    if (Platform.isAndroid) return;
    await _channel.invokeMethod('show');
  }

  static Future<void> hide() async {
    if (Platform.isAndroid) return;
    await _channel.invokeMethod('hide');
  }

  static Future<bool> isShowing() async {
    if (Platform.isAndroid) return false;
    return (await _channel.invokeMethod('isShowing')) ?? false;
  }

  /// 向浮层追加一条请求文本。
  static Future<void> addData(String text) async {
    if (Platform.isAndroid) return;
    await _channel.invokeMethod('addData', text);
  }
}
