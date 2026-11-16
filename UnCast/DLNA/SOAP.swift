import Foundation

/// SOAP 请求解析与响应构造。
///
/// UPnP 控制请求长这样：
/// ```xml
/// <?xml version="1.0"?>
/// <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
///             s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
///   <s:Body>
///     <u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
///       <InstanceID>0</InstanceID>
///       <Speed>1</Speed>
///     </u:Play>
///   </s:Body>
/// </s:Envelope>
/// ```
///
/// 不引入 XMLParser 的完整对象树是因为这里只需要扁平地取 action 名和直接子元素值，
/// 手写解析足够且更可控（客户端会发各种不规范 XML，标准解析器反而容易报错）。
enum SOAP {

    struct Action {
        var service: String = ""
        var name: String = ""
        var args: [String: String] = [:]
    }

    /// 解析 SOAP 动作。service 从 SOAPACTION 头或 xmlns:u 获取。
    static func parse(_ body: Data, soapActionHeader: String?) -> Action? {
        guard let text = String(data: body, encoding: .utf8) else { return nil }

        var action = Action()

        // 优先从 SOAPACTION 头取服务类型与动作名
        // 格式："urn:schemas-upnp-org:service:AVTransport:1#Play"
        if let h = soapActionHeader {
            let cleaned = h.trimmingCharacters(in: CharacterSet(charactersIn: "\" "))
            if let hashIndex = cleaned.firstIndex(of: "#") {
                action.service = String(cleaned[cleaned.startIndex..<hashIndex])
                action.name = String(cleaned[cleaned.index(after: hashIndex)...])
            }
        }

        // 从 <u:ActionName xmlns:u="urn:..."> 提取
        if action.name.isEmpty || action.service.isEmpty {
            if let m = firstMatch(text, pattern: "<(?:\\w+:)?(\\w+)[^>]*xmlns:(?:\\w+)=\"(urn:schemas-upnp-org:service:[^\"]+)\"") {
                action.name = m.0
                action.service = m.1
            }
        }

        guard !action.name.isEmpty else { return nil }

        // 提取该动作元素内的直接子元素作为参数
        let actionPattern = "<(?:\\w+:)?\(NSRegularExpression.escapedPattern(for: action.name))\\b[^>]*>(.*?)</(?:\\w+:)?\(NSRegularExpression.escapedPattern(for: action.name))\\s*>"
        guard let inner = firstMatch(text, pattern: actionPattern, options: [.dotMatchesLineSeparators])?.0 else {
            return action
        }

        action.args = parseChildren(inner)
        return action
    }

    /// 解析 XML 片段中的直接子元素 → [name: value]
    private static func parseChildren(_ xml: String) -> [String: String] {
        var result: [String: String] = [:]
        let pattern = "<(?:[\\w]+:)?([\\w]+)(?:\\s[^>]*)?>(.*?)</(?:[\\w]+:)?\\1\\s*>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]) else {
            return result
        }
        let ns = xml as NSString
        let matches = regex.matches(in: xml, range: NSRange(location: 0, length: ns.length))

        for m in matches {
            guard m.numberOfRanges >= 3 else { continue }
            let name = ns.substring(with: m.range(at: 1))
            var value = ns.substring(with: m.range(at: 2))
            // 参数值可能是被打包的 XML（如 CurrentURIMetaData），先尝试取 CDATA
            if let cdataRange = value.range(of: "<![CDATA["),
               let cdataEnd = value.range(of: "]]>", range: cdataRange.upperBound..<value.endIndex) {
                value = String(value[cdataRange.upperBound..<cdataEnd.lowerBound])
            }
            result[name] = unescape(value)
        }
        return result
    }

    private static func firstMatch(_ text: String, pattern: String, options: NSRegularExpression.Options = []) -> (String, String)? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: options) else { return nil }
        let ns = text as NSString
        guard let m = regex.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)) else { return nil }
        if m.numberOfRanges >= 3 {
            return (ns.substring(with: m.range(at: 1)), ns.substring(with: m.range(at: 2)))
        } else if m.numberOfRanges == 2 {
            return (ns.substring(with: m.range(at: 1)), "")
        }
        return nil
    }

    /// 构造 SOAP 成功响应
    static func successResponse(service: String, action: String, args: [(String, String)]) -> String {
        var argXML = ""
        for (k, v) in args {
            argXML += "<\(k)>\(escape(v))</\(k)>"
        }
        return """
        <?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
        <u:\(action)Response xmlns:u="\(service)">
        \(argXML)
        </u:\(action)Response>
        </s:Body>
        </s:Envelope>
        """
    }

    /// 构造 SOAP 错误响应（UPnPError 401 = Invalid Action 等）
    static func errorResponse(code: Int = 401, description: String = "Invalid Action") -> String {
        """
        <?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
        <s:Body>
        <s:Fault>
        <faultcode>s:Client</faultcode>
        <faultstring>UPnPError</faultstring>
        <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
        <errorCode>\(code)</errorCode>
        <errorDescription>\(escape(description))</errorDescription>
        </UPnPError>
        </detail>
        </s:Fault>
        </s:Body>
        </s:Envelope>
        """
    }

    static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }

    static func unescape(_ s: String) -> String {
        s.replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&apos;", with: "'")
            .replacingOccurrences(of: "&amp;", with: "&")
    }

    /// DIDL-Lite 元数据里取标题，用于界面显示
    static func titleFromDIDL(_ didl: String) -> String? {
        guard let m = firstMatch(didl, pattern: "<dc:title[^>]*>(.*?)</dc:title>", options: [.dotMatchesLineSeparators]) else {
            return nil
        }
        let t = unescape(m.0).trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }
}