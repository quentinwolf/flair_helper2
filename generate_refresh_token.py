import praw
import random
import socket
import sys
import webbrowser

def receive_connection():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("localhost", 8080))
    server.listen(1)
    client = server.accept()[0]
    server.close()
    return client

def send_message(client, message):
    client.send(f"HTTP/1.1 200 OK\r\n\r\n{message}".encode("utf-8"))
    client.close()

def main():
    client_id = input("Enter your client_id: ")
    client_secret = input("Enter your client_secret: ")
    commaScopes = input("Now enter a comma separated list of scopes, or all for all tokens: ")

    if commaScopes.lower() == "all":
        scopes = ["*"]
    else:
        scopes = commaScopes.strip().split(",")

    reddit = praw.Reddit(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        redirect_uri="http://localhost:8080",
        user_agent=f"praw_refresh_token_generator/v0.0.1 by u/quentinwolf",
    )
    state = str(random.randint(0, 65000))
    url = reddit.auth.url(scopes, state, "permanent")
    print(f"Go to this URL: {url}")
    webbrowser.open(url)

    client = receive_connection()
    data = client.recv(1024).decode("utf-8")
    param_tokens = data.split(" ", 2)[1].split("?", 1)[1].split("&")
    params = {
        key: value for (key, value) in [token.split("=") for token in param_tokens]
    }

    if state != params["state"]:
        send_message(
            client,
            f"State mismatch. Expected: {state} Received: {params['state']}",
        )
        sys.exit(1)

    refresh_token = reddit.auth.authorize(params["code"])
    send_message(client, f"Your refresh token is: {refresh_token}")
    print(f"Your refresh token is: {refresh_token}")

if __name__ == "__main__":
    main()
