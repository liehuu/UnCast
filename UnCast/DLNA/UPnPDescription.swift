import Foundation

/// UPnP 设备描述（Device Description）与 Service SCPD 文档生成。
///
/// 标准参考：
/// - UPnP Device Architecture 1.0
/// - DLNA MediaRenderer:1 Device Profile
///
/// 手机端 DLNA 客户端（腾讯视频/爱奇艺/咪咕等）发现设备的流程：
/// 1. SSDP M-SEARCH → 收到 LOCATION
/// 2. GET LOCATION 拿 device description → 解析 deviceType / serviceList
/// 3. 对每个 service GET SCPD → 了解有哪些 action
/// 4. POST /upnp/control/AVTransport 发 SetAVTransportURI + Play
enum UPnPDescription {

    static func deviceXML(uuid: String, friendlyName: String, baseURL: String) -> String {
        """
        <?xml version="1.0" encoding="utf-8"?>
        <root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
          <specVersion>
            <major>1</major>
            <minor>0</minor>
          </specVersion>
          <device>
            <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
            <friendlyName>\(escape(friendlyName))</friendlyName>
            <manufacturer>UnCast</manufacturer>
            <manufacturerURL>https://github.com</manufacturerURL>
            <modelDescription>DLNA Media Renderer for Apple TV</modelDescription>
            <modelName>UnCast</modelName>
            <modelNumber>1.0</modelNumber>
            <serialNumber>\(uuid.prefix(8))</serialNumber>
            <UDN>uuid:\(uuid)</UDN>
            <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
                <SCPDURL>/scpd/AVTransport.xml</SCPDURL>
                <controlURL>/upnp/control/AVTransport</controlURL>
                <eventSubURL>/upnp/event/AVTransport</eventSubURL>
              </service>
              <service>
                <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
                <SCPDURL>/scpd/RenderingControl.xml</SCPDURL>
                <controlURL>/upnp/control/RenderingControl</controlURL>
                <eventSubURL>/upnp/event/RenderingControl</eventSubURL>
              </service>
              <service>
                <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
                <SCPDURL>/scpd/ConnectionManager.xml</SCPDURL>
                <controlURL>/upnp/control/ConnectionManager</controlURL>
                <eventSubURL>/upnp/event/ConnectionManager</eventSubURL>
              </service>
            </serviceList>
          </device>
        </root>
        """
    }

    static func scpdXML(service: String) -> String? {
        switch service {
        case "AVTransport": return avTransportSCPD
        case "RenderingControl": return renderingControlSCPD
        case "ConnectionManager": return connectionManagerSCPD
        default: return nil
        }
    }

    // MARK: - AVTransport SCPD

    private static let avTransportSCPD = """
    <?xml version="1.0" encoding="utf-8"?>
    <scpd xmlns="urn:schemas-upnp-org:service-1-0">
      <specVersion><major>1</major><minor>0</minor></specVersion>
      <actionList>
        <action><name>SetAVTransportURI</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>CurrentURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
            <argument><name>CurrentURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>SetNextAVTransportURI</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>NextURI</name><direction>in</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
            <argument><name>NextURIMetaData</name><direction>in</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>Play</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Speed</name><direction>in</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>Pause</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>Stop</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>Seek</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Unit</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekMode</relatedStateVariable></argument>
            <argument><name>Target</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekTarget</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetTransportInfo</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>CurrentTransportState</name><direction>out</direction><relatedStateVariable>TransportState</relatedStateVariable></argument>
            <argument><name>CurrentTransportStatus</name><direction>out</direction><relatedStateVariable>TransportStatus</relatedStateVariable></argument>
            <argument><name>CurrentSpeed</name><direction>out</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetPositionInfo</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Track</name><direction>out</direction><relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
            <argument><name>TrackDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
            <argument><name>TrackMetaData</name><direction>out</direction><relatedStateVariable>CurrentTrackMetaData</relatedStateVariable></argument>
            <argument><name>TrackURI</name><direction>out</direction><relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
            <argument><name>RelTime</name><direction>out</direction><relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
            <argument><name>AbsTime</name><direction>out</direction><relatedStateVariable>AbsoluteTimePosition</relatedStateVariable></argument>
            <argument><name>RelCount</name><direction>out</direction><relatedStateVariable>RelativeCounterPosition</relatedStateVariable></argument>
            <argument><name>AbsCount</name><direction>out</direction><relatedStateVariable>AbsoluteCounterPosition</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetTransportSettings</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>PlayMode</name><direction>out</direction><relatedStateVariable>CurrentPlayMode</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>SetPlayMode</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>NewPlayMode</name><direction>in</direction><relatedStateVariable>CurrentPlayMode</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetMediaInfo</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>NrTracks</name><direction>out</direction><relatedStateVariable>NumberOfTracks</relatedStateVariable></argument>
            <argument><name>MediaDuration</name><direction>out</direction><relatedStateVariable>CurrentMediaDuration</relatedStateVariable></argument>
            <argument><name>CurrentURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
            <argument><name>CurrentURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
            <argument><name>NextURI</name><direction>out</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
            <argument><name>NextURIMetaData</name><direction>out</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
            <argument><name>PlayMedium</name><direction>out</direction><relatedStateVariable>PlaybackStorageMedium</relatedStateVariable></argument>
            <argument><name>RecordMedium</name><direction>out</direction><relatedStateVariable>RecordStorageMedium</relatedStateVariable></argument>
            <argument><name>WriteStatus</name><direction>out</direction><relatedStateVariable>RecordMediumWriteStatus</relatedStateVariable></argument>
          </argumentList>
        </action>
      </actionList>
      <serviceStateTable>
        <stateVariable sendEvents="no"><name>TransportState</name><dataType>string</dataType>
          <allowedValueList><allowedValue>STOPPED</allowedValue><allowedValue>PLAYING</allowedValue><allowedValue>PAUSED_PLAYBACK</allowedValue><allowedValue>TRANSITIONING</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>TransportStatus</name><dataType>string</dataType>
          <allowedValueList><allowedValue>OK</allowedValue><allowedValue>ERROR_OCCURRED</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType>
          <allowedValueList><allowedValue>1</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>CurrentPlayMode</name><dataType>string</dataType>
          <allowedValueList><allowedValue>NORMAL</allowedValue><allowedValue>REPEAT_ONE</allowedValue><allowedValue>REPEAT_ALL</allowedValue><allowedValue>SHUFFLE</allowedValue></allowedValueList>
          <defaultValue>NORMAL</defaultValue>
        </stateVariable>
        <stateVariable sendEvents="no"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>NextAVTransportURI</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>NextAVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentTrackDuration</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentTrackMetaData</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>RelativeTimePosition</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>AbsoluteTimePosition</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>RelativeCounterPosition</name><dataType>i4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>AbsoluteCounterPosition</name><dataType>i4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentMediaDuration</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>NumberOfTracks</name><dataType>ui4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>PlaybackStorageMedium</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>RecordStorageMedium</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>RecordMediumWriteStatus</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekMode</name><dataType>string</dataType>
          <allowedValueList><allowedValue>REL_TIME</allowedValue><allowedValue>TRACK_NR</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekTarget</name><dataType>string</dataType></stateVariable>
      </serviceStateTable>
    </scpd>
    """

    // MARK: - RenderingControl SCPD

    private static let renderingControlSCPD = """
    <?xml version="1.0" encoding="utf-8"?>
    <scpd xmlns="urn:schemas-upnp-org:service-1-0">
      <specVersion><major>1</major><minor>0</minor></specVersion>
      <actionList>
        <action><name>GetVolume</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
            <argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>SetVolume</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
            <argument><name>DesiredVolume</name><direction>in</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetMute</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
            <argument><name>CurrentMute</name><direction>out</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>SetMute</name>
          <argumentList>
            <argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
            <argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
            <argument><name>DesiredMute</name><direction>in</direction><relatedStateVariable>Mute</relatedStateVariable></argument>
          </argumentList>
        </action>
      </actionList>
      <serviceStateTable>
        <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType><allowedValueRange><minimum>0</minimum><maximum>100</maximum><step>1</step></allowedValueRange></stateVariable>
        <stateVariable sendEvents="no"><name>Mute</name><dataType>boolean</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType>
          <allowedValueList><allowedValue>Master</allowedValue></allowedValueList>
        </stateVariable>
      </serviceStateTable>
    </scpd>
    """

    // MARK: - ConnectionManager SCPD

    private static let connectionManagerSCPD = """
    <?xml version="1.0" encoding="utf-8"?>
    <scpd xmlns="urn:schemas-upnp-org:service-1-0">
      <specVersion><major>1</major><minor>0</minor></specVersion>
      <actionList>
        <action><name>GetProtocolInfo</name>
          <argumentList>
            <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
            <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetCurrentConnectionIDs</name>
          <argumentList>
            <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
          </argumentList>
        </action>
        <action><name>GetCurrentConnectionInfo</name>
          <argumentList>
            <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
            <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
            <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
            <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
            <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
            <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
            <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
            <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
          </argumentList>
        </action>
      </actionList>
      <serviceStateTable>
        <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType>
          <allowedValueList><allowedValue>OK</allowedValue><allowedValue>ContentFormatMismatch</allowedValue><allowedValue>InsufficientBandwidth</allowedValue><allowedValue>UnreliableChannel</allowedValue><allowedValue>Unknown</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType>
          <allowedValueList><allowedValue>Input</allowedValue><allowedValue>Output</allowedValue></allowedValueList>
        </stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
        <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
      </serviceStateTable>
    </scpd>
    """

    /// 声明支持的媒体格式。客户端据此判断能否投屏。
    /// 覆盖主流容器/编码，避免客户端因为「格式不支持」直接不给投。
    static let sinkProtocolInfo: String = {
        let containers = [
            "http-get:*:video/mp4:*",
            "http-get:*:video/x-matroska:*",
            "http-get:*:video/x-msvideo:*",
            "http-get:*:video/mpeg:*",
            "http-get:*:video/quicktime:*",
            "http-get:*:video/webm:*",
            "http-get:*:video/x-flv:*",
            "http-get:*:video/3gpp:*",
            "http-get:*:video/vnd.dlna.mpeg-tts:*",
            "http-get:*:video/MP2T:*",
            "http-get:*:application/vnd.apple.mpegurl:*",
            "http-get:*:application/x-mpegURL:*",
            "http-get:*:audio/mpeg:*",
            "http-get:*:audio/mp4:*",
            "http-get:*:audio/x-wav:*",
            "http-get:*:audio/flac:*",
            "http-get:*:image/jpeg:*",
            "http-get:*:image/png:*",
            "http-get:*:*:*"
        ]
        return containers.joined(separator: ",")
    }()

    static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&apos;")
    }
}