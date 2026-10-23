const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const rootDir = path.resolve(__dirname, '..');
const shortDir = execSync('cmd /c "for %I in (.) do @echo %~fsI"', { cwd: rootDir, encoding: 'utf8' }).trim();
const iconsDir = path.join(shortDir, 'assets', 'icons');

const items = [
    {
        name: 'Start_Bot.lnk',
        thaiName: 'เปิดบอท (เบื้องหลัง).lnk',
        target: 'wscript.exe',
        args: `"${path.join(shortDir, 'scripts', 'start_bot.vbs')}"`,
        icon: path.join(iconsDir, 'start_bot.ico'),
        desc: 'Start Discord Bot in Background'
    },
    {
        name: 'Stop_Bot.lnk',
        thaiName: 'ปิดบอท.lnk',
        target: path.join(shortDir, 'stop_bot.bat'),
        args: '',
        icon: path.join(iconsDir, 'stop_bot.ico'),
        desc: 'Stop Discord Bot'
    },
    {
        name: 'Check_Bot.lnk',
        thaiName: 'ตรวจสอบสถานะบอท.lnk',
        target: path.join(shortDir, 'check_bot.bat'),
        args: '',
        icon: path.join(iconsDir, 'check_bot.ico'),
        desc: 'Check Bot Status'
    },
    {
        name: 'Scan_Stocks.lnk',
        thaiName: 'สแกนหุ้นทันที.lnk',
        target: path.join(shortDir, 'run_scan.bat'),
        args: '',
        icon: path.join(iconsDir, 'scan_stocks.ico'),
        desc: 'Scan Stocks and Send to Discord'
    }
];

let psCommands = `$ws = New-Object -ComObject WScript.Shell\n`;
items.forEach((item, idx) => {
    const lnkPath = path.join(shortDir, item.name);
    psCommands += `
$s${idx} = $ws.CreateShortcut('${lnkPath}')
$s${idx}.TargetPath = '${item.target}'
$s${idx}.Arguments = '${item.args}'
$s${idx}.WorkingDirectory = '${shortDir}'
$s${idx}.IconLocation = '${item.icon},0'
$s${idx}.Description = '${item.desc}'
$s${idx}.Save()
Write-Host "Created ${item.name}"
`;
});

const tempPs = path.join(shortDir, 'temp_sc.ps1');
fs.writeFileSync(tempPs, psCommands, 'utf8');

try {
    const out = execSync(`powershell -ExecutionPolicy Bypass -File "${tempPs}"`, { cwd: shortDir, encoding: 'utf8' });
    console.log(out);

    // Now rename the .lnk files using node fs.renameSync to have beautiful Thai names
    items.forEach(item => {
        const src = path.join(rootDir, item.name);
        const dst = path.join(rootDir, item.thaiName);
        if (fs.existsSync(src)) {
            if (fs.existsSync(dst)) fs.unlinkSync(dst);
            fs.renameSync(src, dst);
            console.log(`Renamed to: ${item.thaiName}`);
        }
    });

} catch (err) {
    console.error('Error:', err);
} finally {
    if (fs.existsSync(tempPs)) fs.unlinkSync(tempPs);
}
