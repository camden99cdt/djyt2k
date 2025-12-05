# main.py
import tkinter as tk
from gui import YTDemucsApp


def main():
    root = tk.Tk()
    app = YTDemucsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
