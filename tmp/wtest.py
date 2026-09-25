import threading, time, webview
def close_soon():
    time.sleep(4)
    for w in webview.windows: w.destroy()
threading.Thread(target=close_soon, daemon=True).start()
w = webview.create_window("Window Test", "data:text/html,<h1>hello</h1>", width=500, height=300)
print("created; starting...", flush=True)
webview.start()
print("window closed cleanly", flush=True)
