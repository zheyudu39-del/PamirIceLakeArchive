"""截取正在运行的 GUI 窗口。"""

import ctypes
import ctypes.wintypes as wt
import struct
import sys

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()

TITLE = "帕米尔冰湖档案"


def find_window(title):
    """找标题匹配且尺寸最大的可见窗口。

    同一进程会同时有主窗口和若干子/工具窗口（实测抓到一个 237x39 的
    标题条）。按面积取最大的是最稳的判据，比按枚举顺序取第一个可靠。
    """
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title in buf.value:
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    area = (rect.right - rect.left) * (rect.bottom - rect.top)
                    found.append((area, hwnd))
        return True

    user32.EnumWindows(cb, 0)
    found.sort(reverse=True)
    return [hwnd for _, hwnd in found]


def capture(hwnd, out):
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    print(f"窗口 {w}x{h}")

    hwindc = user32.GetWindowDC(hwnd)
    srcdc = gdi32.CreateCompatibleDC(hwindc)
    bmp = gdi32.CreateCompatibleBitmap(hwindc, w, h)
    gdi32.SelectObject(srcdc, bmp)
    user32.PrintWindow(hwnd, srcdc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BIH()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(srcdc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    with open(out, "wb") as f:
        f.write(struct.pack("<2sIHHI", b"BM", 54 + len(buf), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0,
                            len(buf), 2835, 2835, 0, 0))
        f.write(buf.raw)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(srcdc)
    user32.ReleaseDC(hwnd, hwindc)
    print("saved", out)


if __name__ == "__main__":
    out = sys.argv[1]
    if len(sys.argv) > 2:
        # 优先按句柄：同名窗口可能有多个，句柄是唯一确定的
        hwnd = int(sys.argv[2], 0)
        print(f"使用指定句柄 {hwnd}")
        capture(hwnd, out)
    else:
        wins = find_window(TITLE)
        if not wins:
            print("找不到窗口")
            sys.exit(1)
        for h in wins[:3]:
            rect = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            print(f"候选 {h}: {rect.right - rect.left}x{rect.bottom - rect.top}")
        capture(wins[0], out)
