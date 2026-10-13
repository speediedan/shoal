import os, pty, sys, time, select
SH = sys.argv[1]
def run(keys, default):
    pid, fd = pty.fork()
    if pid == 0:
        sys.path.insert(0, SH + "/ui")
        import ui_term
        r = ui_term.read_line("> ", default)
        sys.stdout.write("\nRESULT=" + repr(r) + "\n"); sys.stdout.flush(); os._exit(0)
    time.sleep(0.3)
    for k in keys:
        os.write(fd, k.encode()); time.sleep(0.08)
    out = b""
    end = time.time() + 2
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try: out += os.read(fd, 4096)
            except OSError: break
    os.waitpid(pid, 0)
    return out.decode(errors="replace").split("RESULT=")[-1].strip()
E = "\x1b"
# move left 6 (to before "-07:00"), Ctrl+A/home, type, end, delete-back, paste
print(run([E + "[D"] * 6 + ["X", E + "[H", "Y", E + "[F", "\x7f", E + "[200~pasted" + E + "[201~", "\r"], "2026-10-13T09:00-07:00"))
print(run([E], "abc"))
print(run(["\r"], "keep"))
print(run([E + "[H", E + "[3~", "\r"], "zabc"))
