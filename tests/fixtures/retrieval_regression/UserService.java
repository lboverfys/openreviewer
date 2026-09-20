package sample;

class UserService {
    UserMapper mapper;

    User load(long id) {
        return mapper.getById(id);
    }
}
