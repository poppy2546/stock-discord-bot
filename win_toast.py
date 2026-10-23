import subprocess
import base64
from xml.sax.saxutils import escape as xml_escape

def send_windows_notification(buys, regime, phase="trading"):
    if phase == "morning":
        title = "🌅 สรุปตลาดและแนวโน้มประจำวัน (07:00 น.)"
        body = f"ตลาดโหมด {regime} สรุปภาพรวมและแนวโน้มหลังตลาดปิด (รอบเช้าไม่มีคำสั่งซื้อ)"
    elif phase == "prep":
        if buys:
            buy_tickers = ", ".join([str(b.get('ticker', '')).strip() for b in buys])
            title = f"🎯 แผนเตรียมการก่อนเปิดตลาด: {buy_tickers}"
            body = f"ตลาดโหมด {regime} หุ้นตัวเต็งที่ควรเตรียมแผนตั้งรับ: {buy_tickers}"
        else:
            title = "🎯 แผนเตรียมการก่อนเปิดตลาด (20:00 น.)"
            body = f"ตลาดโหมด {regime} ยังไม่มีหุ้นเข้าเกณฑ์เด่น แนะนำเตรียมถือเงินสด"
    else:
        if buys:
            buy_tickers = ", ".join([str(b.get('ticker', '')).strip() for b in buys])
            title = f"📢 แจ้งเตือนหุ้นควรซื้อวันนี้: {buy_tickers}"
            body = f"ตลาดอยู่ในโหมด {regime} หุ้นที่ผ่านเกณฑ์: {buy_tickers} (คลิกดูรายละเอียด)"
        else:
            title = "📊 รายงานหุ้นประจำวัน (23:00 น.)"
            body = f"ตลาดโหมด {regime} วันนี้ไม่มีหุ้นที่ผ่านเกณฑ์ซื้อ แนะนำถือเงินสด (WAIT)"

    safe_title = xml_escape(title)
    safe_body = xml_escape(body)
    
    xml_template = f"""<toast>
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_body}</text>
        </binding>
    </visual>
</toast>"""

    b64_xml = base64.b64encode(xml_template.encode("utf-8")).decode("ascii")

    ps_script = """
    param([string]$payload)
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $xmlStr = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($payload))
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($xmlStr)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("StockAdvisor").Show($toast)
    } catch {
        # Suppress notification failure silently
    }
    """

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script, "-payload", b64_xml],
            capture_output=True,
            timeout=5
        )
    except Exception:
        pass
