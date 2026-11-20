"""Keep desktop windows comfortably sized within the current display."""
def size_window(window, width, height, min_width, min_height):
    available_width = max(320, window.winfo_screenwidth() - 50)
    available_height = max(260, window.winfo_screenheight() - 100)
    window.minsize(min(min_width, available_width), min(min_height, available_height))
    window.geometry(f"{min(width, available_width)}x{min(height, available_height)}")
