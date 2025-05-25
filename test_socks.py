import requests
sess = requests.Session()
sess.verify = False
 # Если у вас именно SOCKS5 с удалённым резолвом, используйте socks5h
sess.proxies = {
   "http":  "socks5h://142.132.169.43:40167",
   "https": "socks5h://142.132.169.43:40167",
 }
try:
   r = sess.get("https://api.ipify.org?format=json", timeout=10)
   print("OK:", r.text)
except Exception as e:
   print("ERROR:", e)
