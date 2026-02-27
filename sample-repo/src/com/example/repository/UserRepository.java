package com.example.repository;

import com.example.model.User;
import java.util.HashMap;
import java.util.Map;

public class UserRepository {
    private Map<String, User> users = new HashMap<>();

    public User findById(String userId) {
        // Returns null if not found - caller should handle
        return users.get(userId);
    }

    public User save(User user) {
        users.put(user.getId(), user);
        return user;
    }

    public void delete(User user) {
        users.remove(user.getId());
    }

    public boolean exists(String userId) {
        return users.containsKey(userId);
    }
}
