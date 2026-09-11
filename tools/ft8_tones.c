// ft8_tones: read FT8 message texts on stdin, print "OK <79 tones> <text>" per line.
#include <stdio.h>
#include <string.h>
#include "ft8/message.h"
#include "ft8/encode.h"
int main(void) {
    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!*line) continue;
        ftx_message_t msg; ftx_message_init(&msg);
        ftx_message_rc_t rc = ftx_message_encode(&msg, NULL, line);
        if (rc != FTX_MESSAGE_RC_OK) { printf("ERR %d %s\n", (int)rc, line); continue; }
        uint8_t tones[79]; ft8_encode(msg.payload, tones);
        printf("OK ");
        for (int i = 0; i < 79; i++) printf("%d", tones[i]);
        printf(" %s\n", line);
    }
    return 0;
}
