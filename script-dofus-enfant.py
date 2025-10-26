import psutil, win32gui, win32process

def titles(pid):
    t = []
    def cb(hwnd, t):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            name = win32gui.GetWindowText(hwnd)
            if name and "release" in name.lower():  # filtre ici
                t.append(name)
        return True
    win32gui.EnumWindows(cb, t)
    return t

for p in psutil.process_iter(['pid', 'name']):
    if (p.info['name'] or '').lower() == 'dofus.exe':
        for t in titles(p.info['pid']):
            print(t)
        for c in p.children():
            for t in titles(c.pid):
                print(t)
        break
