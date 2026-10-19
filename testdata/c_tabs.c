// Tab indents, attached braces, tight keywords.
#include <stdio.h>

#define MAX_N 1000

static int fib(int n) {
	if (n <= 1) return n;
	return fib(n - 1) + fib(n - 2);
}

int main(void) {
	int i;
	int sum = 0;
	for (i = 0; i < MAX_N; i++) {
		sum += fib(i % 20);
	}
	printf("sum=%d\n", sum);
	return 0;
}
