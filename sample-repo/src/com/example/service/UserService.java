package com.example.service;

import com.example.model.User;
import com.example.repository.UserRepository;

public class UserService {
    private UserRepository repository;

    public UserService(UserRepository repository) {
        this.repository = repository;
    }

    public User getUser(String userId) {
        // Bug: doesn't check if user exists before accessing
        User user = repository.findById(userId);
        return user.normalize();  // NPE if user is null
    }

    public User createUser(String name, String email) {
        User user = new User(name, email);
        return repository.save(user);
    }

    public void deleteUser(String userId) {
        User user = getUser(userId);
        repository.delete(user);
    }
}
