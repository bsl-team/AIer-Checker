// Deliberately inconsistent sample: mixed indent units, mixed brace placement,
// mixed keyword spacing, trailing whitespace and an over-long row.
#include <stdio.h>
#include <stdlib.h>

int good(int n) {
    int acc = 0;
    for (int i = 0; i < n; i++) {
        acc += i;
    }
    return acc;
}

int messy(int n)
{
	int acc = 0;
   int extra = 0;  
    if(n > 0) {
        acc = n;
    }
    else {
        acc = -n;
    }
    for (int i = 0; i < n; i++)
    {
        extra += i * 2;
    }
    char *label = "this line is deliberately much longer than the limit that the rest of this sample file keeps to";
    return acc + extra;
}

int main(void) {
    int r = good(10);
    if (r > 0) {
        printf("r=%d\n", r);
    } else
    {
        printf("zero\n");
    }
    return 0;
}
