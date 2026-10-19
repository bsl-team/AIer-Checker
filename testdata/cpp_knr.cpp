// K&R style, 4 spaces, attached braces.
#include <cstdio>
#include <string>
#include <vector>

namespace demo {

class Solver {
public:
    int solve(std::vector<int> values, int target) {
        int total = 0;
        for (size_t i = 0; i < values.size(); i++) {
            if (values[i] <= target) {
                total += values[i];
            }
        }
        return total;
    }

    void report(const std::string &title) const {
        printf("%s: ok\n", title.c_str());
    }

private:
    std::vector<int> cache_;
};

}  // namespace demo

int main(int argc, char **argv) {
    Solver solver;
    std::vector<int> data = {1, 2, 3, 4, 5};
    int answer = solver.solve(data, 3);
    if (answer > 0) {
        printf("answer=%d\n", answer);
    } else {
        printf("none\n");
    }
    return 0;
}
