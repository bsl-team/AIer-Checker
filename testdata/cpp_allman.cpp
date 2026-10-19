// Allman style, 4 spaces, braces on their own row.
#include <cstdio>

struct Point
{
    int x;
    int y;
};

int distance_sq(struct Point a, struct Point b)
{
    int dx = a.x - b.x;
    int dy = a.y - b.y;
    return dx * dx + dy * dy;
}

int main(void)
{
    struct Point p = {1, 2};
    struct Point q = {4, 6};
    if (distance_sq(p, q) > 10)
    {
        printf("far\n");
    }
    else
    {
        printf("near\n");
    }
    return 0;
}
