#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include "libvdeplug.h"
#include "libvdemgmt.h"

static VDECONN *connect_port(char *socket, int port) {
    struct vde_open_args args = {.port = port};
    VDECONN *conn = vde_open(socket, "native-hub-check", &args);
    assert(conn != NULL);
    return conn;
}

static void frame(VDECONN *from, VDECONN **to, size_t count, size_t len) {
    unsigned char sent[9000], received[9000];
    for (size_t i = 0; i < len; i++) sent[i] = (unsigned char)(i * 17 + len);
    sent[0] = 0x02; /* An unknown unicast must still be flooded by a hub. */
    assert(vde_send(from, sent, len, 0) == (ssize_t)len);
    for (size_t i = 0; i < count; i++) {
        struct pollfd fd = {.fd = vde_datafd(to[i]), .events = POLLIN};
        assert(poll(&fd, 1, 2000) == 1);
        assert(vde_recv(to[i], received, sizeof(received), 0) == (ssize_t)len);
        assert(memcmp(sent, received, len) == 0);
        assert(poll(&fd, 1, 20) == 0); /* Exactly one copy. */
    }
    struct pollfd own = {.fd = vde_datafd(from), .events = POLLIN};
    assert(poll(&own, 1, 20) == 0); /* No reflection to the sender. */
}

int main(int argc, char **argv) {
    assert(argc == 2);
    alarm(30);
    char directory[] = "/tmp/native-hub-check-XXXXXX";
    assert(mkdtemp(directory));
    char socket[256], management[256];
    snprintf(socket, sizeof(socket), "%s/hub", directory);
    snprintf(management, sizeof(management), "%s/mgmt", directory);
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        execl(argv[1], argv[1], "--nostdin", "-n", "65535", "-x", "-s", socket, "-M", management, (char *)NULL);
        _exit(127);
    }
    struct vdemgmt *mgmt = NULL;
    for (int i = 0; i < 200 && !mgmt; i++) {
        usleep(10000);
        mgmt = vdemgmt_open(management);
    }
    assert(mgmt);
    VDECONN *low = connect_port(socket, 1), *other = connect_port(socket, 2);
    VDECONN *two[] = {other};
    frame(low, two, 1, 64);
    VDECONN *high = connect_port(socket, 65534); /* Allocated after traffic began. */
    VDECONN *three[] = {other, high};
    frame(low, three, 2, 1514);
    frame(low, three, 2, 9000);
    VDECONN *reverse[] = {low, other};
    frame(high, reverse, 2, 64);
    assert(vdemgmt_sendcmd(mgmt, "port/setnumports 16", NULL) == EADDRINUSE);
    vde_close(high);
    usleep(50000);
    assert(vdemgmt_sendcmd(mgmt, "port/remove 65534", NULL) == 0);
    assert(vdemgmt_sendcmd(mgmt, "port/setnumports 65520", NULL) == 0);
    frame(low, two, 1, 64);
    assert(vdemgmt_sendcmd(mgmt, "port/setnumports 16", NULL) == 0);
    frame(low, two, 1, 1514);
    assert(vdemgmt_sendcmd(mgmt, "port/setnumports 65535", NULL) == 0);
    high = connect_port(socket, 65534);
    three[1] = high;
    frame(low, three, 2, 9000);
    frame(high, reverse, 2, 64);
    vde_close(high);
    vde_close(other);
    vde_close(low);
    vdemgmt_close(mgmt);
    kill(pid, SIGTERM);
    assert(waitpid(pid, NULL, 0) == pid);
    unlink(management);
    rmdir(socket);
    rmdir(directory);
    puts("PASS: native flooding, no duplicates/reflection, jumbo, late port 65534, shrink/grow");
    return 0;
}
